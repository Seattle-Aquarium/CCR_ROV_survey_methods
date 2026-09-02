--[[
  UTC RAW develop -- the Lightroom Classic half of the GPR batch.

  A manifest and nothing else. Lightroom evaluates this file to decide whether
  the plugin can be loaded at all, and it is not a normal Lua environment:
  code here cannot be relied on to have `io`, `os`, or even `pcall`. An earlier
  version of this file wrote a diagnostic line before the return, and Lightroom
  reported the whole plugin as "malfunctioning and can not be used" -- with no
  indication of which line was at fault. Anything that needs to *do* something
  belongs in Init.lua, which runs in the full plugin environment.

  Installed by utc/lightroom/install.py and registered once through
  Lightroom's Plug-in Manager. Edit the copy in the repo, not the installed
  one, and bump PLUGIN_VERSION there so the installed copy is replaced.
]]

return {
    -- Matches the host. This plugin uses LrPhoto:applyDevelopSettings and
    -- LrCatalog:setSelectedPhotos, neither of which is ancient, so there is
    -- nothing to gain from claiming an older SDK.
    LrSdkVersion = 14.0,
    LrSdkMinimumVersion = 6.0,

    LrToolkitIdentifier = 'org.seattleaquarium.utc.rawdevelop',
    LrPluginName = 'UTC RAW develop',

    LrInitPlugin = 'Init.lua',

    -- The entry point, and the reason this plugin can be started at all.
    --
    -- Lightroom initialises a plugin lazily: not at startup, but the first
    -- time something asks for one of the entry points the plugin declares.
    -- A plugin that declares none is registered, enabled, shown as "Installed
    -- and running" in the Plug-in Manager -- and never executes a line. This
    -- menu item is what UTC drives to start a batch.
    -- LrExportMenuItems, not LrLibraryMenuItems. Both are documented as
    -- adding entries under File > Plug-in Extras; only this one actually
    -- does in Lightroom Classic 14.5.1. The library variant produces
    -- "(none defined)" in the submenu, in every module, with no error and
    -- nothing to distinguish it from a plugin that failed to load.
    LrExportMenuItems = {
        {
            title = 'Run UTC RAW batch',
            file = 'Run.lua',
        },
    },

    -- Run LrInitPlugin before the menu item fires rather than alongside it,
    -- so Init.lua's diagnostics are already in place when work starts.
    LrForceInitPlugin = true,

    VERSION = { major = 1, minor = 0, revision = 0 },
}
