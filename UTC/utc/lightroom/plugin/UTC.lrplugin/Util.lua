--[[
  Talking to the Python side, and writing down what happened.

  The two halves hand off through plain files in a per-run directory. The
  format is one `key=value` per line -- not JSON, because the SDK ships no
  JSON encoder and a hand-rolled one is a bug farm for no gain. Keys are
  identifiers, values are the rest of the line, so a Windows path with spaces,
  backslashes or an equals sign in it survives untouched.

  Marker files carry the handoffs that have no payload (denoise_ready,
  denoise_done, cancel): their existence is the whole message.
]]

local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'

local M = {}

local LrTasks = import 'LrTasks'

--- pcall that a yielding SDK call can survive.
--
-- Lua 5.1 cannot yield across a pcall boundary, and most of the interesting
-- SDK calls -- addPhoto, applyDevelopSettings, getRawMetadata -- yield
-- internally. Wrapping one in plain pcall does not catch its errors; it makes
-- it fail, every time, with "AgEventLoop.yieldToScheduler called when
-- yielding is not allowed". LrTasks.pcall is the yield-safe equivalent.
function M.pcall(fn)
    if LrTasks and LrTasks.pcall then
        return LrTasks.pcall(fn)
    end
    return pcall(fn)
end

--- Where Python leaves the pointer to the current run.
-- Built from the home directory rather than %LOCALAPPDATA% so both halves
-- agree even when the environment variable is redirected; Python writes the
-- pointer to both spellings for the same reason.
function M.handoffFile()
    local home = LrPathUtils.getStandardFilePath('home')
    local p = LrPathUtils.child(home, 'AppData')
    p = LrPathUtils.child(p, 'Local')
    p = LrPathUtils.child(p, 'UTC')
    p = LrPathUtils.child(p, 'lightroom')
    return LrPathUtils.child(p, 'current_job.txt')
end

function M.exists(path)
    return path ~= nil and LrFileUtils.exists(path) ~= false
end

--- Read a key=value file into a table. Returns nil if it is not there.
function M.readKV(path)
    if not M.exists(path) then return nil end
    local text = LrFileUtils.readFile(path)
    if not text or text == '' then return nil end
    local out = {}
    for line in string.gmatch(text, '[^\r\n]+') do
        local k, v = string.match(line, '^([%w_]+)=(.*)$')
        if k then out[k] = v end
    end
    return out
end

function M.num(kv, key, fallback)
    local v = kv and kv[key]
    local n = v and tonumber(v)
    if n == nil then return fallback end
    return n
end

function M.bool(kv, key, fallback)
    local v = kv and kv[key]
    if v == nil then return fallback end
    return v == '1' or v == 'true' or v == 'yes'
end

--- Every `group=WxH|left|top|right|bottom` line, keyed by "WxH".
-- Python computes the crop arithmetic once, in one place, and hands us the
-- answer; we verify it against what Lightroom actually did.
function M.readGroups(path)
    local out = {}
    if not M.exists(path) then return out end
    local text = LrFileUtils.readFile(path) or ''
    for line in string.gmatch(text, '[^\r\n]+') do
        local body = string.match(line, '^group=(.*)$')
        if body then
            local key, l, t, r, b = string.match(
                body, '^([%dx]+)|([%d%.]+)|([%d%.]+)|([%d%.]+)|([%d%.]+)$')
            if key then
                out[key] = { left = tonumber(l), top = tonumber(t),
                             right = tonumber(r), bottom = tonumber(b) }
            end
        end
    end
    return out
end

-- ---------------------------------------------------------------- writing

local function writeText(path, text)
    local fh, err = io.open(path, 'wb')
    if not fh then return false, err end
    fh:write(text)
    fh:close()
    return true
end

M.runDir = nil
M.logPath = nil

function M.openRun(runDir)
    M.runDir = runDir
    M.logPath = LrPathUtils.child(runDir, 'plugin.log')
end

--- Append a line to the run's log. This file is the only account of what
-- Lightroom did, so it is kept even when the run directory is cleaned up
-- after a failure.
function M.log(msg)
    if not M.logPath then return end
    -- Never allowed to raise. The plugin environment does not guarantee `io`
    -- or `os`, and a logger that throws turns every run into a failure whose
    -- only symptom is the logging itself.
    pcall(function()
        local fh = io.open(M.logPath, 'ab')
        if not fh then return end
        local when = ''
        pcall(function() when = os.date('%H:%M:%S') .. '  ' end)
        fh:write(when .. tostring(msg) .. '\n')
        fh:close()
    end)
end

--- Publish the current phase. Written to a temporary name and moved into
-- place so a poller never reads half a file.
function M.status(fields)
    if not M.runDir then return end
    local parts = {}
    for _, key in ipairs({ 'phase', 'done', 'total', 'message', 'error' }) do
        local v = fields[key]
        if v ~= nil then
            table.insert(parts, key .. '=' .. tostring(v))
        end
    end
    local text = table.concat(parts, '\n') .. '\n'
    local final = LrPathUtils.child(M.runDir, 'status.txt')
    local tmp = LrPathUtils.child(M.runDir, 'status.tmp')
    if writeText(tmp, text) then
        if LrFileUtils.exists(final) then LrFileUtils.delete(final) end
        LrFileUtils.move(tmp, final)
    end
end

function M.marker(name)
    if not M.runDir then return nil end
    return LrPathUtils.child(M.runDir, name)
end

function M.touch(name)
    local p = M.marker(name)
    if p then writeText(p, 'x') end
end

function M.hasMarker(name)
    local p = M.marker(name)
    return p ~= nil and M.exists(p)
end

return M
