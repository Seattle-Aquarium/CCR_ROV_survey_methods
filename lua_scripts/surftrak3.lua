--[[
  Sticky rangefinder target for ArduSub SURFTRAK mode.

  Works with ArduSub 4.5.X.

  The sticky rangefinder target...
  * starts as nil
  * gets set the first time the sub is armed and in SURFTRAK mode
  * is rounded to the nearest increment (SCR_USER2)
  * can be set to a pre-defined value (SCR_USER1) via a joystick button (script_1)
  * can be adjusted up/down via joystick buttons (script_2, script_3)
  * is re-applied in SURFTRAK mode if the pilot uses the vertical stick to hop over an obstacle
  * is reset to nil if the sub is disarmed

  Target changes via buttons are only accepted when armed, in SURFTRAK, rf is healthy, and below SURFTRAK_DEPTH.

  SURFTRAK will publish a NAMED_VALUE_FLOAT with RFTarget; this script will do the same for all other modes.

  Example joystick button settings:
  BTN14_SFUNCTION     108         # shift-dpad-right   script_1: set rf_target to SCR_USER1
  BTN11_SFUNCTION     109         # shift-dpad-up      script_2: increment rf_target by SCR_USER2
  BTN12_SFUNCTION     110         # shift-dpad-down    script_3: decrement rf_target by SCR_USER2

  Parameters:
  SCR_USER1           Default rangefinder target in cm
  SCR_USER2           Increment / decrement step in cm
  SURFTRAK_DEPTH      Depth ceiling in cm
--]]

local SURFTRAK_MODE_NUM = 21    -- ArduSub SURFTRAK mode number
local RF_MIN_CM = 50            -- Minimum rangefinder target (0.5 m)
local RF_MAX_CM = 5000          -- Maximum rangefinder target (50.0 m)

local UPDATE_MS = 100           -- Update rate: 10 Hz (100 ms)
local BAD_PARAM_MS = 10000      -- Retry period if parameters are missing/invalid (10 s)

-- Parameters
local scr_user1_p = Parameter("SCR_USER1")
local scr_user2_p = Parameter("SCR_USER2")
local surftrak_depth_p = Parameter("SURFTRAK_DEPTH")

-- Controller state variables
local rf_target_cm              -- Sticky rangefinder target in cm
local last_rf_target_sent_ms = 0

local function clamp(val, min_val, max_val)
  if val < min_val then return min_val end
  if val > max_val then return max_val end
  return val
end

local function round(val, inc)
  return math.floor(val / inc + 0.5) * inc
end

local function clamp_round(val, inc, min_val, max_val)
  return clamp(round(val, inc), min_val, max_val)
end

-- Return current Z position relative to origin (NEU frame, Z up, cm)
-- Future: add Lua binding for inertial_nav.get_position_z_up_cm()
local function get_position_z_up_cm()
  -- This method requires an EKF origin
  local current_pos = ahrs:get_relative_position_NED_origin()
  if current_pos then
    -- NED in meters (down) to NEU in cm (up)
    return -current_pos:z() * 100.0
  end

  -- Fallback: get barometer altitude
  return baro:get_altitude() * 100.0
end

-- Return true if sub is at or shallower than SURFTRAK_DEPTH
local function above_surftrak_depth()
  return get_position_z_up_cm() >= surftrak_depth_p:get()
end

-- Send RFTarget to GCS
local function send_rf_target_telemetry()
  if not rf_target_cm then return end
  local now_ms = millis()
  if (now_ms - last_rf_target_sent_ms):tofloat() >= 250 then
    last_rf_target_sent_ms = now_ms
    gcs:send_named_float("RFTarget", rf_target_cm * 0.01)
  end
end

local function update()
  -- Get and check parameters
  local action_target_cm = scr_user1_p:get()
  if action_target_cm < RF_MIN_CM then
    gcs:send_text(6, "surftrak3: SCR_USER1 is too low (min 50 cm)")
    return update, BAD_PARAM_MS
  end

  local action_inc_cm = scr_user2_p:get()
  if action_inc_cm < 1 then
    gcs:send_text(6, "surftrak3: SCR_USER2 must be 1 (1cm) or more")
    return update, BAD_PARAM_MS
  end

  -- Get and clear button counts in all flight modes so that a stale press won't cause confusion later
  local count_set = sub:get_and_clear_button_count(1)
  local count_inc = sub:get_and_clear_button_count(2)
  local count_dec = sub:get_and_clear_button_count(3)
  sub:get_and_clear_button_count(4)

  -- Forget the target if the sub is disarmed
  if not arming:is_armed() then
    rf_target_cm = nil
    return update, UPDATE_MS
  end

  -- Wait for SURFTRAK to set a target
  local curr_target_cm = sub:get_rangefinder_target_cm()
  if curr_target_cm < 0 then
    return update, UPDATE_MS
  end

  -- Broadcast RFTarget if outside SURFTRAK mode
  if vehicle:get_mode() ~= SURFTRAK_MODE_NUM then
    send_rf_target_telemetry()
    return update, UPDATE_MS
  end

  -- Do nothing if the rangefinder is unhealthy or the ROV is above SURFTRAK_DEPTH
  if not sub:rangefinder_alt_ok() or above_surftrak_depth() then
    return update, UPDATE_MS
  end

  -- Init rf_target_cm
  if rf_target_cm == nil then
    rf_target_cm = clamp_round(curr_target_cm, action_inc_cm, RF_MIN_CM, RF_MAX_CM)
    gcs:send_text(6, string.format("surftrak3: init rangefinder target to %.2f m", rf_target_cm / 100.0))
  end

  -- Apply joystick actions
  if count_set > 0 then
    rf_target_cm = clamp_round(action_target_cm, action_inc_cm, RF_MIN_CM, RF_MAX_CM)
  else
    local net_inc = count_inc - count_dec
    if net_inc ~= 0 then
      rf_target_cm = clamp_round(rf_target_cm + net_inc * action_inc_cm, action_inc_cm, RF_MIN_CM, RF_MAX_CM)
    end
  end

  -- Apply rf_target_cm
  if math.abs(curr_target_cm - rf_target_cm) >= 1.0 then
    sub:set_rangefinder_target_cm(rf_target_cm)
  end

  return update, UPDATE_MS
end

gcs:send_text(6, "surftrak3.lua loaded")
return update, UPDATE_MS
