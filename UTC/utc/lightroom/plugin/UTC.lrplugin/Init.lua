--[[
  Plugin entry point. Runs once, at Lightroom start.

  Almost every launch of Lightroom on this machine has nothing to do with UTC,
  so the first thing this does is look for a job and return. No job, no cost,
  no trace -- the operator's ordinary use of the application is untouched.

  When there is a job, the pointer to it is deleted before any work begins.
  That matters: the pointer is the only thing standing between "Lightroom
  starts and processes a folder" and "Lightroom reprocesses that folder every
  time it is opened for the rest of the year".

  ## The two marks

  A plugin that never runs and a plugin that runs but cannot write are
  indistinguishable from outside, and both look like "Lightroom did nothing".
  So this leaves two marks by different means:

  * ``plugin_ran/`` -- a *directory*, created through LrFileUtils. Needs no
    plain-Lua file handles and no `_PLUGIN`.
  * ``plugin_boot.log`` -- a file, written with `io.open`.

  Neither depends on `_PLUGIN`, which is not reliably populated this early;
  an earlier version keyed both marks off `_PLUGIN.path` and so recorded
  nothing whether the plugin ran or not. Both paths come from
  LrPathUtils.getStandardFilePath, which is available as soon as the SDK is.
]]

local okPath, LrPathUtils = pcall(function() return import 'LrPathUtils' end)
local okFile, LrFileUtils = pcall(function() return import 'LrFileUtils' end)

--- ``<home>/AppData/Local/UTC/lightroom`` -- the folder both halves agree on.
local function utcRoot()
    local p = LrPathUtils.getStandardFilePath('home')
    for _, part in ipairs({ 'AppData', 'Local', 'UTC', 'lightroom' }) do
        p = LrPathUtils.child(p, part)
    end
    return p
end

local ROOT = okPath and select(2, pcall(utcRoot)) or nil

-- Mark one: a directory, made with the SDK's own file API.
if okPath and okFile and ROOT then
    pcall(function()
        LrFileUtils.createAllDirectories(LrPathUtils.child(ROOT, 'plugin_ran'))
    end)
end

--- Mark two: a log line, written with plain Lua. Never allowed to raise --
-- this is the diagnostic, so it must not become the fault it reports.
local function boot(msg)
    pcall(function()
        if not ROOT then return end
        local fh = io.open(LrPathUtils.child(ROOT, 'plugin_boot.log'), 'ab')
        if not fh then return end
        local when = ''
        pcall(function() when = os.date('%Y-%m-%d %H:%M:%S') .. '  ' end)
        fh:write(when .. tostring(msg) .. '\n')
        fh:close()
    end)
end

boot('--- init running ---')

if not okPath or not okFile then
    boot('FAILED to import LrPathUtils/LrFileUtils')
    return
end

local okCtx, LrFunctionContext = pcall(function()
    return import 'LrFunctionContext'
end)
if not okCtx then
    boot('FAILED to import LrFunctionContext: ' .. tostring(LrFunctionContext))
    return
end

local okUtil, Util = pcall(require, 'Util')
local okJob, Job = pcall(require, 'Job')
if not okUtil or not okJob then
    boot('FAILED to load modules: Util=' .. tostring(okUtil) ..
         ' (' .. tostring(Util) .. ') Job=' .. tostring(okJob) ..
         ' (' .. tostring(Job) .. ')')
    return
end
boot('modules loaded')

-- Nothing else happens here. The batch is started by the menu item in
-- Run.lua, because Lightroom only initialises a plugin when one of its
-- declared entry points is used -- LrInitPlugin alone never fires.

