--[[
surftrak2.lua: SURFTRAK joystick control

This script manages the rangefinder target in SURFTRAK mode. The pilot uses joystick buttons to set the _desired_
rangefinder target; the script will monitor and overwrite the _current_ rangefinder target to match. The desired
rangefinder target is forgotten when you exit SURFTRAK mode.

Parameters:
    SCR_USER1: default rangefinder target distance in cm (e.g., 200)
    SCR_USER2: increment/decrement step size in cm (e.g., 10)

Example bindings:
    BTN14_SFUNCTION 108  # shift-dpad-right button sets the rangefinder target to SCR_USER1
    BTN11_SFUNCTION 109  # shift-dpad-up button increments the target by SCR_USER2
    BTN12_SFUNCTION 110  # shift-dpad-down button decrements the target by SCR_USER2
]]--

local SURFTRAK_MODE_NUM = 21    -- SURFTRAK mode number
local LOWER_LIMIT_CM = 50       -- Minimum rangefinder target

local UPDATE_MS = 100           -- Time between updates
local BAD_PARAM_MS = 10000      -- Use a long update if we are waiting for good parameters

local desired_target            -- The desired rangefinder target

local function set_target(next_target_cm)
    if next_target_cm < LOWER_LIMIT_CM then
        gcs:send_text(6, "surftrak_buttons.lua: hit lower limit")
        next_target_cm = LOWER_LIMIT_CM
    end
    sub:set_rangefinder_target_cm(next_target_cm)
end

local function update()
    -- Get and clear button counts in all flight modes so that a stale press won't cause confusion later
    local count_set = sub:get_and_clear_button_count(1)
    local count_inc = sub:get_and_clear_button_count(2)
    local count_dec = sub:get_and_clear_button_count(3)

    -- Forget the desired target if we are not in SURFTRAK mode
    if vehicle:get_mode() ~= SURFTRAK_MODE_NUM and desired_target ~= nil then
        desired_target = nil
    end

    -- Silently do nothing if we are disarmed, in the wrong flight mode, or the rangefinder is unhealthy
    if not arming:is_armed() or vehicle:get_mode() ~= SURFTRAK_MODE_NUM or not sub:rangefinder_alt_ok() then
        return update, UPDATE_MS
    end

    -- Get and check parameters
    local action_target_cm = param:get('SCR_USER1')
    if action_target_cm == nil then
        gcs:send_text(6, "surftrak_buttons.lua: set SCR_USER1 to rangefinder target, in cm")
        return update, BAD_PARAM_MS
    end

    if action_target_cm < LOWER_LIMIT_CM then
        gcs:send_text(6, "surftrak_buttons.lua: SCR_USER1 is too low")
        return update, BAD_PARAM_MS
    end

    local action_inc_cm = param:get('SCR_USER2')
    if action_inc_cm == nil then
        gcs:send_text(6, "surftrak_buttons.lua: set SCR_USER2 to rangefinder target increment value, in cm")
        return update, BAD_PARAM_MS
    end

    if action_inc_cm < 1 then
        gcs:send_text(6, "surftrak_buttons.lua: SCR_USER2 must be 1 (1cm) or more")
        return update, BAD_PARAM_MS
    end

    -- Set or change the desired target
    if count_set > 0 then
        desired_target = action_target_cm
    else
        if desired_target ~= nil then
            local net_inc = count_inc - count_dec
            if net_inc ~= 0 then
                desired_target = desired_target + net_inc * action_inc_cm
            end
        end
    end

    -- Apply the desired target
    if desired_target ~= nil then
        local curr_target_cm = sub:get_rangefinder_target_cm()
        if curr_target_cm ~= desired_target then
            set_target(desired_target)
        end
    end

    return update, UPDATE_MS
end

gcs:send_text(6, "surftrak2.lua running")

return update()
