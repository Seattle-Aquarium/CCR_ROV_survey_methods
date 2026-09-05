--[[
  The entry point: File > Plug-in Extras > Run UTC RAW batch.

  Lightroom will not run a plugin just because it is installed, registered and
  enabled -- it initialises one lazily, the first time something asks for an
  entry point the plugin declares. An earlier design had only LrInitPlugin and
  no entry points at all, and so never executed a single line while the
  Plug-in Manager cheerfully reported "Installed and running". This menu item
  is the thing UTC drives to start a batch.

  It is deliberately quiet when there is nothing to do. An operator who finds
  this in the menu and clicks it out of curiosity gets one dialog saying there
  is no job waiting, not a mysterious no-op and not a stack trace.
]]

local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrFunctionContext = import 'LrFunctionContext'
local LrPathUtils = import 'LrPathUtils'
local LrTasks = import 'LrTasks'

local Util = require 'Util'
local Job = require 'Job'

--- Pick up the job UTC left, or explain that there is not one.
local function start()
    local pointer = Util.handoffFile()
    if not Util.exists(pointer) then
        LrDialogs.message(
            'UTC RAW develop',
            'There is no batch waiting. Start one from UTC: Process photos > '
            .. 'Develop and export.', 'info')
        return
    end

    local runDir = LrFileUtils.readFile(pointer)
    runDir = runDir and (string.gsub(runDir, '^%s*(.-)%s*$', '%1')) or ''

    -- Consume the pointer first, whatever happens next. It is the only thing
    -- standing between "run this folder once" and "run it again every time".
    pcall(function() LrFileUtils.delete(pointer) end)

    if runDir == '' or not LrFileUtils.exists(runDir) then
        LrDialogs.message('UTC RAW develop',
                          'The batch folder has gone: ' .. tostring(runDir),
                          'critical')
        return
    end

    local jobFile = LrPathUtils.child(runDir, 'job.txt')
    local job = Util.readKV(jobFile)
    if not job or not job.source_dir or not job.tif_dir then
        LrDialogs.message('UTC RAW develop',
                          'The batch description could not be read: '
                          .. tostring(jobFile), 'critical')
        return
    end

    Util.openRun(runDir)
    Util.log('UTC RAW develop starting')
    Util.log('  source: ' .. tostring(job.source_dir))
    Util.log('  output: ' .. tostring(job.tif_dir))
    Util.status{ phase = 'started', done = 0, total = 0,
                 message = 'Lightroom is starting the batch' }

    Job.run(job)
end

-- On a task of its own: the batch takes minutes to hours, and a menu item
-- that blocks the main thread would freeze the application it has to drive.
LrTasks.startAsyncTask(function()
    LrFunctionContext.callWithContext('UTC RAW develop', function(context)
        context:addFailureHandler(function(_, message)
            Util.log('FAILED: ' .. tostring(message))
            Util.status{ phase = 'error', error = tostring(message) }
        end)
        start()
    end)
end)
