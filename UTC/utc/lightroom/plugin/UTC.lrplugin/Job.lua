--[[
  The batch, start to finish.

  Import the folder's GPRs into this run's own scratch catalog, apply the crop
  and Remove Chromatic Aberration, prove the crop landed on the exact pixel
  size asked for, hand the screen over for AI Denoise, then export 16-bit
  ProPhoto TIFs.

  Two things are worth knowing before reading on.

  **The crop is verified, not assumed.** Lightroom stores a crop as fractions
  of the frame and rounds the result, so a rectangle that is arithmetically
  perfect can still land a pixel out. One photo per distinct frame size is used
  as a probe: apply, read back croppedDimensions, adjust, repeat. Only the
  settled rectangle is applied to the rest. If it will not settle the run stops
  *before* Denoise, which is the expensive part.

  **Denoise is not scriptable.** There is no SDK entry point for it in any
  current version, and since 14.5 no batch entry point either -- it lives in
  the Develop module's Detail panel. So the plugin parks here while Python
  turns Denoise on for one photo and uses Synchronize Settings to carry it to
  the rest. Parking uses LrTasks.sleep rather than a modal dialog on purpose:
  a modal would block the very interface the automation has to click.
]]

local LrApplication = import 'LrApplication'
local LrApplicationView = import 'LrApplicationView'
local LrExportSession = import 'LrExportSession'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrTasks = import 'LrTasks'

local Util = require 'Util'

local M = {}

--- Photos per write transaction and per export batch. Small enough that a
-- Stop is honoured promptly, large enough that the per-transaction overhead
-- stays in the noise.
local CHUNK = 10

--- Give up parking rather than leaving Lightroom held open forever if the
-- Python side dies mid-Denoise.
local DENOISE_TIMEOUT = 6 * 60 * 60

local function trim(s)
    return (string.gsub(tostring(s or ''), '^%s*(.-)%s*$', '%1'))
end

-- ------------------------------------------------------------------ import

local function gprFiles(dir)
    local out = {}
    if not LrFileUtils.exists(dir) then return out end
    for path in LrFileUtils.files(dir) do
        local ext = string.lower(LrPathUtils.extension(path) or '')
        if ext == 'gpr' then table.insert(out, path) end
    end
    table.sort(out)
    return out
end

--- Import every path, inside a write block that is allowed to yield.
--
-- The scheduler error these calls used to fail with came from wrapping them
-- in a plain `pcall`, which Lua 5.1 will not let a yield cross -- not from
-- the write block. `withProlongedWriteAccessDo` also works, but it puts up a
-- "requesting permission to write to your catalog" dialog and waits for a
-- click, which stalls an unattended run indefinitely.
local function importAll(catalog, paths)
    local photos = {}
    local failed = 0
    catalog:withWriteAccessDo('UTC import', function()
        do
            for _, path in ipairs(paths) do
                local ok, photo = Util.pcall(function()
                    return catalog:addPhoto(path)
                end)
                if ok and photo then
                    table.insert(photos, photo)
                else
                    failed = failed + 1
                    Util.log('could not import ' .. path .. ': ' ..
                             tostring(photo))
                end
                Util.status{ phase = 'importing', done = #photos,
                             total = #paths, message = 'importing GPR' }
            end
        end
    end, { timeout = 300 })
    return photos, failed
end

-- -------------------------------------------------------------------- crop

local function sizeKey(w, h)
    return string.format('%dx%d', math.floor(w), math.floor(h))
end

local function frameOf(photo)
    local ok, dims = Util.pcall(function()
        return photo:getRawMetadata('dimensions')
    end)
    if ok and dims and dims.width and dims.height then return dims end
    return nil
end

local function croppedOf(photo)
    local ok, dims = Util.pcall(function()
        return photo:getRawMetadata('croppedDimensions')
    end)
    if ok and dims and dims.width and dims.height then return dims end
    return nil
end

local function centred(frameW, frameH, wantW, wantH)
    local l = (1.0 - wantW / frameW) / 2.0
    local t = (1.0 - wantH / frameH) / 2.0
    return { left = l, top = t, right = 1.0 - l, bottom = 1.0 - t }
end

local function settingsFor(rect, removeCA)
    local s = {
        CropLeft = rect.left,
        CropTop = rect.top,
        CropRight = rect.right,
        CropBottom = rect.bottom,
        CropAngle = 0,
        CropConstrainToWarp = 0,
        HasCrop = true,
    }
    s.AutoLateralCA = removeCA and 1 or 0
    return s
end

--- Apply settings to one photo. Prefers LrPhoto:applyDevelopSettings, which
-- takes a plain table; falls back to a plugin-owned develop preset on builds
-- where that method is absent.
local function applyTo(catalog, photos, settings, label)
    local preset = nil
    -- Util.pcall, not pcall: applyDevelopSettings yields, and Lua 5.1 will
    -- not let a yield cross a plain pcall boundary.
    catalog:withWriteAccessDo(label, function()
        do
            for _, photo in ipairs(photos) do
                local ok, err = Util.pcall(function()
                    photo:applyDevelopSettings(settings, label)
                end)
                if not ok then
                    if not preset then
                        Util.log('applyDevelopSettings unavailable (' ..
                                 tostring(err) .. '); using a develop preset')
                        preset = LrApplication.addDevelopPresetForPlugin(
                            _PLUGIN, 'UTC ' .. label, settings)
                    end
                    Util.pcall(function()
                        photo:applyDevelopPreset(preset, _PLUGIN)
                    end)
                end
            end
        end
    end, { timeout = 300 })
end

--- Find a rectangle that Lightroom really does round to wantW x wantH.
-- Starts from the rectangle Python computed and corrects against what
-- Lightroom reports, so the two never have to agree about rounding.
local function settleRect(catalog, probe, rect, frame, wantW, wantH, removeCA)
    local r = { left = rect.left, top = rect.top,
                right = rect.right, bottom = rect.bottom }
    for attempt = 1, 8 do
        applyTo(catalog, { probe }, settingsFor(r, removeCA), 'UTC crop probe')
        local got = croppedOf(probe)
        if not got then
            return nil, 'Lightroom did not report a cropped size'
        end
        if got.width == wantW and got.height == wantH then
            Util.log(string.format(
                'crop settled after %d attempt(s): %.6f/%.6f/%.6f/%.6f -> %dx%d',
                attempt, r.left, r.top, r.right, r.bottom, got.width, got.height))
            return r, nil
        end
        Util.log(string.format('probe attempt %d gave %dx%d, wanted %dx%d',
                               attempt, got.width, got.height, wantW, wantH))
        r.right = r.right + (wantW - got.width) / frame.width
        r.bottom = r.bottom + (wantH - got.height) / frame.height
        if r.right > 1.0 or r.bottom > 1.0 or r.right <= r.left
                or r.bottom <= r.top then
            return nil, 'the requested crop does not fit this frame'
        end
    end
    return nil, string.format('could not land on %dx%d after 8 attempts',
                              wantW, wantH)
end

local function cropAll(catalog, photos, groups, wantW, wantH, removeCA)
    -- Bucket by native frame size: one probe per size, not per photo.
    local byKey, order = {}, {}
    local noDims = 0
    for _, photo in ipairs(photos) do
        local frame = frameOf(photo)
        if frame then
            local key = sizeKey(frame.width, frame.height)
            if not byKey[key] then
                byKey[key] = { frame = frame, photos = {} }
                table.insert(order, key)
            end
            table.insert(byKey[key].photos, photo)
        else
            noDims = noDims + 1
        end
    end
    if noDims > 0 then
        Util.log(noDims .. ' photo(s) reported no dimensions and were skipped')
    end

    local cropped = 0
    for _, key in ipairs(order) do
        local bucket = byKey[key]
        local rect = groups[key]
                     or centred(bucket.frame.width, bucket.frame.height,
                                wantW, wantH)
        Util.status{ phase = 'cropping', done = cropped, total = #photos,
                     message = 'crop ' .. key }
        local settled, err = settleRect(catalog, bucket.photos[1], rect,
                                        bucket.frame, wantW, wantH, removeCA)
        if not settled then
            return cropped, string.format('%s frames: %s', key, err)
        end
        local settings = settingsFor(settled, removeCA)
        local i = 1
        while i <= #bucket.photos do
            local last = math.min(i + CHUNK - 1, #bucket.photos)
            local slice = {}
            for k = i, last do table.insert(slice, bucket.photos[k]) end
            applyTo(catalog, slice, settings, 'UTC crop')
            i = last + 1
            Util.status{ phase = 'cropping', done = cropped + last,
                         total = #photos, message = 'crop ' .. key }
        end
        cropped = cropped + #bucket.photos
    end

    -- Prove it, photo by photo, before anything expensive happens.
    local wrong = 0
    for _, photo in ipairs(photos) do
        local got = croppedOf(photo)
        if not got or got.width ~= wantW or got.height ~= wantH then
            wrong = wrong + 1
        end
    end
    if wrong > 0 then
        return cropped, string.format(
            '%d of %d photo(s) are not %dx%d after cropping',
            wrong, #photos, wantW, wantH)
    end
    return cropped, nil
end

-- ----------------------------------------------------------------- denoise

local function parkForDenoise(catalog, photos)
    -- Select everything so the automation only has to invoke Enhance.
    pcall(function()
        LrApplicationView.switchToModule('library')
        LrApplicationView.showView('grid')
    end)
    Util.pcall(function()
        catalog:setSelectedPhotos(photos[1], photos)
    end)

    Util.touch('denoise_ready')
    Util.status{ phase = 'awaiting_denoise', done = 0, total = #photos,
                 message = 'AI Denoise running in Lightroom' }

    local waited = 0
    while not Util.hasMarker('denoise_done') do
        if Util.hasMarker('cancel') then return false, 'stopped' end
        LrTasks.sleep(0.4)
        waited = waited + 0.4
        if waited > DENOISE_TIMEOUT then
            return false, 'timed out waiting for Denoise'
        end
    end
    return true, nil
end

-- ------------------------------------------------------------------ export

local COMPRESSION = {
    zip = 'compressionMethod_ZIP',
    lzw = 'compressionMethod_LZW',
    none = 'compressionMethod_None',
}

local function exportSettings(job)
    return {
        LR_export_destinationType = 'specificFolder',
        LR_export_destinationPathPrefix = job.tif_dir,
        LR_export_useSubfolder = false,
        LR_format = 'TIFF',
        LR_export_bitDepth = Util.num(job, 'bit_depth', 16),
        LR_export_colorSpace = trim(job.color_space) ~= '' and job.color_space
                               or 'ProPhotoRGB',
        LR_tiff_compressionMethod =
            COMPRESSION[string.lower(trim(job.tiff_compression))]
            or 'compressionMethod_ZIP',
        LR_size_doConstrain = false,
        LR_outputSharpeningOn = false,
        LR_useWatermark = false,
        LR_renamingTokensOn = false,
        LR_reimportExportedPhoto = false,
        LR_includeVideoFiles = false,
        LR_embeddedMetadataOption = 'all',
        LR_removeLocationMetadata = false,
        LR_collisionHandling = Util.bool(job, 'overwrite', false)
                               and 'overwrite' or 'rename',
    }
end

local function exportAll(photos, job)
    local settings = exportSettings(job)
    LrFileUtils.createAllDirectories(job.tif_dir)
    local done = 0
    local i = 1
    while i <= #photos do
        if Util.hasMarker('cancel') then return done, true end
        local last = math.min(i + CHUNK - 1, #photos)
        local slice = {}
        for k = i, last do table.insert(slice, photos[k]) end
        local session = LrExportSession{
            photosToExport = slice,
            exportSettings = settings,
        }
        session:doExportOnCurrentTask()
        done = done + #slice
        i = last + 1
        Util.status{ phase = 'exporting', done = done, total = #photos,
                     message = 'exporting TIF' }
    end
    return done, false
end

-- -------------------------------------------------------------------- main

function M.run(job)
    local catalog = LrApplication.activeCatalog()
    Util.log('catalog: ' .. tostring(catalog:getPath()))

    local wantW = math.floor(Util.num(job, 'crop_w', 4606))
    local wantH = math.floor(Util.num(job, 'crop_h', 4030))
    local removeCA = Util.bool(job, 'remove_ca', true)

    local paths = gprFiles(job.source_dir)
    Util.log(#paths .. ' GPR file(s) in ' .. tostring(job.source_dir))
    if #paths == 0 then
        Util.status{ phase = 'error', error = 'no GPR files in ' .. tostring(job.source_dir) }
        return
    end
    Util.status{ phase = 'importing', done = 0, total = #paths }

    local photos, failed = importAll(catalog, paths)
    if #photos == 0 then
        Util.status{ phase = 'error', error = 'Lightroom imported none of the GPR files' }
        return
    end
    Util.log(#photos .. ' imported, ' .. failed .. ' failed')
    Util.status{ phase = 'imported', done = #photos, total = #paths }

    if Util.hasMarker('cancel') then
        Util.status{ phase = 'stopped', done = 0, total = #photos }
        return
    end

    local groups = Util.readGroups(LrPathUtils.child(Util.runDir, 'job.txt'))
    local cropped, cropErr = cropAll(catalog, photos, groups, wantW, wantH, removeCA)
    if cropErr then
        Util.log('CROP FAILED: ' .. cropErr)
        Util.status{ phase = 'error', done = cropped, total = #photos,
                     error = cropErr }
        return
    end
    Util.status{ phase = 'cropped', done = cropped, total = #photos }

    if Util.bool(job, 'denoise', true) then
        -- Only the Enhance command will do.
        --
        -- A develop preset *can* carry an Enhance/Denoise filter, and applying
        -- one through the SDK is tempting: it would replace all of the UI
        -- driving below. It does not work. The preset writes the setting into
        -- the catalog -- the photo reads as denoised, and every catalog-level
        -- check agrees -- but no denoise is computed and the exported pixels
        -- come out bit-for-bit identical to an un-denoised export. Measured:
        -- 100% identical pixels, max difference 1/65535, and a run fast enough
        -- to prove no GPU work happened. The preset's CompressedSettings hash
        -- refers to cached data belonging to the photo it was built from;
        -- applied elsewhere there is nothing behind it.
        local ok, err = parkForDenoise(catalog, photos)
        if not ok then
            Util.log('denoise stage ended: ' .. tostring(err))
            Util.status{ phase = (err == 'stopped') and 'stopped' or 'error',
                         done = 0, total = #photos, error = err }
            return
        end
    else
        Util.log('denoise skipped by request')
    end

    if Util.hasMarker('cancel') then
        Util.status{ phase = 'stopped', done = 0, total = #photos }
        return
    end

    Util.status{ phase = 'exporting', done = 0, total = #photos }
    local exported, stopped = exportAll(photos, job)
    Util.log(exported .. ' TIF(s) exported to ' .. tostring(job.tif_dir))
    Util.status{ phase = stopped and 'stopped' or 'done',
                 done = exported, total = #photos,
                 message = exported .. ' TIF exported' }
end

return M
