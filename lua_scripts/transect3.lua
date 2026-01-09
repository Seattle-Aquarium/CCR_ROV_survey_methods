--[[
    Run with a transect with a super-sticky rangefinder target. Use GUIDED, SURFTRAK and other modes as necessary.

    The rangefinder target:
    * starts as None
    * gets set the first time you enter GUIDED or SURFTRAK modes
    * gets set to the nearest 10 cm
    * can be adjusted up/down in 10 cm increments
    * is stick across all modes
    * is reset to None if the sub is disarmed

    The GUIDED speed:
    * starts at 10 cms
    * can be adjusted up/down in 10 cms increments

    Example joystick button settings:
    param set BTN0_FUNCTION    1.0         # shift
    param set BTN11_SFUNCTION  108         # shift-dpad-up      script_1: increment rf_target by 10 cm
    param set BTN12_SFUNCTION  109         # shift-dpad-down    script_2: decrement rf_target by 10 cm
    param set BTN13_SFUNCTION  111         # shift-dpad-right   script_4: increment speed by 10 cm/s
    param set BTN14_SFUNCTION  110         # shift-dpad-left    script_3: decrement speed by 10 cm/s
    param set BTN2_SFUNCTION   11          # shift-X            GUIDED mode
]]--

local surftrak_depth_p = Parameter()
local wpnav_speed_p = Parameter()

if not surftrak_depth_p:init('SURFTRAK_DEPTH') or not wpnav_speed_p:init('WPNAV_SPEED') then
  gcs:send_text(3, "transect.lua: parameters missing, exit")
  return
end

local UPDATE_TIME_MS = 100      -- update time
local FUTURE_S = 10             -- target is a point along the heading, distance = speed * FUTURE_S

local GUIDED_MODE_NUM = 4       -- sub GUIDED mode number
local SURFTRAK_MODE_NUM = 21    -- sub SURFTRAK mode number
local ROTATION_PITCH_270 = 25   -- down-facing

local SPEED_INC_CMS = 10        -- inc / dec WPNAV_SPEED by this amount per button press
local SPEED_MIN_CMS = 10
local SPEED_MAX_CMS = 150

local RF_TARGET_INC = 0.1       -- inc / dec rf_target by this amount per button press
local RF_TARGET_MIN = 0.5
local RF_TARGET_MAX = 50.0

local BTN_INC_RF_TARGET = 1     -- joystick script button assignments
local BTN_DEC_RF_TARGET = 2     -- Lua indices 1..4 map to joystick button functions 108..111
local BTN_INC_SPEED = 3
local BTN_DEC_SPEED = 4

local prev_mode                 -- track mode changes
local rf_target                 -- rangefinder target

local function clamp(val, min, max)
  if val < min then return min end
  if val > max then return max end
  return val
end

local function above_surftrak_depth()
  -- Return true if we are above SURFTRAK_DEPTH (that is, too close to the surface)
  return baro:get_altitude() * 100.0 > surftrak_depth_p:get()
end

local function set_rf_target(proposed_rf_target)
  -- Round the proposed target to the nearest 0.1m, this helps the pilot set specific targets
  rf_target = clamp(math.floor(proposed_rf_target * 10 + 0.5) / 10, RF_TARGET_MIN, RF_TARGET_MAX)
  gcs:send_text(6, string.format("transect.lua: set rangefinder target to %.2f m", rf_target))
end

local function respond_to_joystick_buttons()
  -- Get and clear button counts
  local count = {}
  for i = 1, 4 do
    count[i] = sub:get_and_clear_button_count(i)
  end

  -- Increment or decrement WPNAV_SPEED
  -- Changes take effect the next time the pilot enters GUIDED mode
  local net_inc = count[BTN_INC_SPEED] - count[BTN_DEC_SPEED]
  if net_inc ~= 0 then
    wpnav_speed_p:set(clamp(wpnav_speed_p:get() + net_inc * SPEED_INC_CMS, SPEED_MIN_CMS, SPEED_MAX_CMS))
    gcs:send_text(6, string.format("transect.lua: change WPNAV_SPEED to %.0f cms", wpnav_speed_p:get()))
  end

  if rf_target == nil then
    return
  end

  -- Increment or decrement rf_target
  net_inc = count[BTN_INC_RF_TARGET] - count[BTN_DEC_RF_TARGET]
  if net_inc ~= 0 then
    set_rf_target(rf_target + net_inc * RF_TARGET_INC)
  end
end

local function set_posvel_target(pos)
  -- Call vehicle:set_target_posvel_terrain, return true if successful
  local vel_fwd = wpnav_speed_p:get() * 0.01
  local heading = ahrs:get_yaw_rad()

  -- Project forward along heading to calc target xy position
  local dist = vel_fwd * FUTURE_S
  local target_pos = Vector3f()
  target_pos:x(pos:x() + dist * math.cos(heading))
  target_pos:y(pos:y() + dist * math.sin(heading))
  target_pos:z(rf_target)

  local target_vel = Vector3f()
  target_vel:x(vel_fwd * math.cos(heading))
  target_vel:y(vel_fwd * math.sin(heading))
  target_vel:z(0)

  return vehicle:set_target_posvel_terrain(target_pos, target_vel)
end

local function update_guided_mode(rf_reading)
  local pos = ahrs:get_relative_position_NED_origin()

  if prev_mode ~= GUIDED_MODE_NUM then
    -- Alert the pilot to problems just once
    -- Rangefinder must be healthy, see ArduSub/mode_guided.cpp
    if rf_reading == nil then
      gcs:send_text(4, "transect.lua: waiting for a rangefinder reading")
    end

    -- Must have a position
    if pos == nil then
      gcs:send_text(4, "transect.lua: waiting for EFK position")
    end

    -- Must be below SURFTRAK_DEPTH
    if above_surftrak_depth() then
      gcs:send_text(4, "transect.lua: dive below SURFTRAK depth to engage script")
    end
  end

  if rf_reading == nil or pos == nil or above_surftrak_depth() then
    return
  end

  -- Initialize rf_target
  if rf_target == nil then
    set_rf_target(rf_reading)
  end

  -- Send rf_target to the GCS
  gcs:send_named_float("RFTarget", rf_target)

  -- All good, send a posvel target
  local success = set_posvel_target(pos)
  
  -- Log results in a GUIded Terrain table
  logger:write("GUIT",
      "RFTarg,RFRead,Head,RFOk,SubT,Succ",
      "fffBfB",
      rf_target or 0,
      rf_reading or 0,
      ahrs:get_yaw_rad(),
      sub:rangefinder_alt_ok() and 1 or 0,
      sub:get_rangefinder_target_cm() * 0.01,
      success and 1 or 0
  )
end

local function update_surftrak_mode(rf_reading)
  -- Must be below SURFTRAK_DEPTH
  if above_surftrak_depth() then
    return
  end

  -- Initialize rf_target
  if rf_target == nil and rf_reading then
    set_rf_target(rf_reading)
  end

  if rf_target == nil then
    return
  end

  -- Apply sticky rf_target
  if rf_target * 100 ~= sub:get_rangefinder_target_cm() then
    sub:set_rangefinder_target_cm(rf_target * 100)
  end
end

local function update()
  respond_to_joystick_buttons()

  -- Must be armed
  if not arming:is_armed() then
    -- Reset rf target
    if rf_target ~= nil then
      rf_target = nil
      gcs:send_text(6, "transect.lua: forget rangefinder target")
    end
    return update, UPDATE_TIME_MS
  end

  -- Get a rangefinder reading
  local rf_reading
  if sub:rangefinder_alt_ok() then
    rf_reading = rangefinder:distance_orient(ROTATION_PITCH_270)
  end

  local mode = vehicle:get_mode()
  if mode == GUIDED_MODE_NUM then
    update_guided_mode(rf_reading)
  elseif mode == SURFTRAK_MODE_NUM then
    update_surftrak_mode(rf_reading)
  end
  prev_mode = mode

  return update, UPDATE_TIME_MS
end

gcs:send_text(6, "transect.lua loaded")

return update()
