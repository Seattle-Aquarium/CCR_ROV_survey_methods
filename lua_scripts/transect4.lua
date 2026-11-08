--[[
    Use GUIDED mode to move forward at a constant speed while following the terrain.
    Maintain a sticky rangefinder target across all modes.

    Works with ArduSub 4.7.2.

    The rangefinder target...
    * starts as nil
    * gets set the first time you enter GUIDED or SURFTRAK mode
    * is rounded to the nearest increment (GAT_RF_INC)
    * can be adjusted up/down via joystick buttons
    * is sticky across all modes
    * is reset to nil if the sub is disarmed

    Pilot controls:
    * Switch modes as necessary
    * Change heading using the joystick yaw
    * Increment / decrement altitude using joystick buttons
    * Increment / decrement speed using joystick buttons

    Example joystick button settings:
    param set BTN0_FUNCTION    1.0         # shift
    param set BTN2_SFUNCTION   11          # shift-X            GUIDED mode
    param set BTN3_SFUNCTION   13          # shift-Y            SURFTRAK mode
    param set BTN11_SFUNCTION  109         # shift-dpad-up      script_2: increment rf_target by GAT_RF_INC
    param set BTN12_SFUNCTION  110         # shift-dpad-down    script_3: decrement rf_target by GAT_RF_INC
    param set BTN13_SFUNCTION  111         # shift-dpad-left    script_4: decrement speed by GAT_SPD_INC
    param set BTN14_SFUNCTION  108         # shift-dpad-right   script_1: increment speed by GAT_SPD_INC

    There are several custom GAT parameters:
    * GAT_SPD sets the forward speed during GUIDED mode (default 0.2 m/s)
    * GAT_SPD_INC sets the speed increment (default 0.05 m/s)
    * GAT_RF_INC sets the rangefinder target increment (default 0.05 m)
    * GAT_CLR_MIN sets the hard-stop minimum floor clearance in meters (default 0.40 m)
    * GAT_CLR_SLOW sets the floor clearance slowdown threshold in meters (default 0.65 m)

    Custom parameters are a bit different than normal parameters:
    * This script sets the default values; these default values are _not_ stored in the eeprom.
    * This script uses "set()" (vs "set_and_save()") to modify the values in memory; these changes are not stored.
    * The pilot can set these in the GCS to specific values; these changes _will_ be stored in the eeprom.
    * If you use "--wipe" in sim_vehicle.py, you can't use "--add-param-file" to set these parameters.
]]--

local PARAM_TABLE_KEY = 96
local PARAM_TABLE_PREFIX = "GAT_"

assert(param:add_table(PARAM_TABLE_KEY, PARAM_TABLE_PREFIX, 5), "transect: could not add param table")
assert(param:add_param(PARAM_TABLE_KEY, 1, "SPD", 0.2), "transect: could not add SPD param")
assert(param:add_param(PARAM_TABLE_KEY, 2, "SPD_INC", 0.05), "transect: could not add SPD_INC param")
assert(param:add_param(PARAM_TABLE_KEY, 3, "CLR_MIN", 0.40), "transect: could not add CLR_MIN param")
assert(param:add_param(PARAM_TABLE_KEY, 4, "CLR_SLOW", 0.65), "transect: could not add CLR_SLOW param")
assert(param:add_param(PARAM_TABLE_KEY, 5, "RF_INC", 0.05), "transect: could not add RF_INC param")

local surftrak_depth_p = Parameter("SURFTRAK_DEPTH")
local gat_spd_p = Parameter("GAT_SPD")
local gat_spd_inc_p = Parameter("GAT_SPD_INC")
local gat_rf_inc_p = Parameter("GAT_RF_INC")
local gat_clr_min_p = Parameter("GAT_CLR_MIN")
local gat_clr_slow_p = Parameter("GAT_CLR_SLOW")
local wp_spd_p = Parameter("WP_SPD")
local wp_spd_up_p = Parameter("WP_SPD_UP")
local wp_spd_dn_p = Parameter("WP_SPD_DN")
local wp_acc_z_p = Parameter("WP_ACC_Z")
local rcmap_yaw_p = Parameter("RCMAP_YAW")

local RUN_HZ = 20                   -- update frequency (20 Hz)
local MAX_DT = 0.5                  -- cap dt to avoid large integration step on stall
local ZERO_VEC = Vector3f()

local GUIDED_MODE_NUM = 4           -- sub GUIDED mode number
local SURFTRAK_MODE_NUM = 21        -- sub SURFTRAK mode number
local ROTATION_PITCH_270 = 25       -- downward-facing rangefinder orientation

-- Configuration constants for PosVelAccel controller
local ACC_XY = 0.5                  -- horizontal acceleration limit in m/s^2
local ACC_Z = 0.5                   -- fallback vertical acceleration limit in m/s^2
local P_GAIN_Z = 1.0                -- proportional gain for vertical terrain following

-- Pilot controls
local SPEED_MIN = 0.1
local RF_TARGET_MIN = 0.5
local RF_TARGET_MAX = 50.0

local BTN_INC_RF_TARGET = 2         -- Script button 2 (Function 109)
local BTN_DEC_RF_TARGET = 3         -- Script button 3 (Function 110)
local BTN_DEC_SPEED = 4             -- Script button 4 (Function 111)
local BTN_INC_SPEED = 1             -- Script button 1 (Function 108)

-- Controller state variables
local prev_mode                     -- track vehicle mode transitions
local rf_target                     -- sticky rangefinder target in meters
local guided_active = false
local surftrak_active = false
local last_time_ms = millis()
local yaw_target_rad = 0
local pos_target = Vector3f()       -- 3D position target in m (NED)
local vel_target = Vector3f()       -- 3D velocity target in m/s (NED)
local acc_target = Vector3f()       -- 3D acceleration target in m/s^2 (NED)
local commanded_speed = 0           -- dynamic commanded forward speed after clearance scaling
local rf_lost = false               -- true if rangefinder reading has dropped out (>1.0s)
local last_valid_rf                 -- cached last valid rangefinder reading

-- Timestamps for timeouts
local last_rf_time_ms = 0           -- timestamp of last valid rangefinder reading

-- Message rate limiters
local last_low_clr_warn_ms = 0      -- rate limiter for low clearance warning
local last_rf_lost_warn_ms = 0      -- rate limiter for rangefinder lost warning
local last_prereq_warn_ms = 0       -- rate limiter for guided prerequisites warning
local last_rf_target_sent_ms = 0    -- rate limiter for RFTarget telemetry (4 Hz)
local last_rf_target_sent_val = -1

local function clamp(val, min_val, max_val)
  if val < min_val then return min_val end
  if val > max_val then return max_val end
  return val
end

local function round(val, inc)
  return math.floor(val / inc + 0.5) * inc
end

-- Clamp initial GAT_SPD if it exceeds WP_SPD
local init_cruise_speed = gat_spd_p:get()
if init_cruise_speed > wp_spd_p:get() then
  gat_spd_p:set(wp_spd_p:get())
end

-- Throttled RFTarget telemetry sender (sends immediately on change or at 4 Hz)
local function send_rf_target_telemetry(force)
  if not rf_target then return end
  local now_ms = millis()
  if force or rf_target ~= last_rf_target_sent_val or (now_ms - last_rf_target_sent_ms):tofloat() >= 250 then
    last_rf_target_sent_ms = now_ms
    last_rf_target_sent_val = rf_target
    gcs:send_named_float("RFTarget", rf_target)
  end
end

-- If sub is shallower than SURFTRAK_DEPTH, return true
local function above_surftrak_depth()
  local min_depth = math.abs(surftrak_depth_p:get()) * 0.01

  local current_pos = ahrs:get_relative_position_NED_origin()
  if current_pos then
    -- current_pos:z() is positive down (depth in meters) in NED
    return current_pos:z() < min_depth
  end

  -- Fallback to barometer if EKF origin is not yet initialized during early startup
  local alt = baro:get_altitude()
  return alt and (-alt < min_depth) or false
end

-- Only set SURFTRAK target if SURFTRAK mode is active and sub is below SURFTRAK depth
local function set_surftrak_target_cm(target_cm)
  if vehicle:get_mode() == SURFTRAK_MODE_NUM and not above_surftrak_depth() then
    return sub:set_rangefinder_target_cm(target_cm)
  end
  return false
end

-- Set rangefinder target, rounded to nearest GAT_RF_INC
local function set_rf_target(proposed_rf_target)
  local rf_inc = math.max(0.01, gat_rf_inc_p:get())
  local rounded_target = round(proposed_rf_target, rf_inc)
  rf_target = clamp(rounded_target, RF_TARGET_MIN, RF_TARGET_MAX)
  gcs:send_text(6, string.format("transect: set rangefinder target to %.2f m", rf_target))

  -- SURFTRAK sends RFTarget, so we don't need to
  if vehicle:get_mode() ~= SURFTRAK_MODE_NUM then
    send_rf_target_telemetry(true)
  end
end

local function respond_to_joystick_buttons()
  -- Increment or decrement GAT_SPD (clamped to WP_SPD)
  local net_speed_inc = sub:get_and_clear_button_count(BTN_INC_SPEED) - sub:get_and_clear_button_count(BTN_DEC_SPEED)
  if net_speed_inc ~= 0 then
    local speed_inc = math.max(0.01, gat_spd_inc_p:get())
    local max_speed = wp_spd_p:get()
    local cur_speed = gat_spd_p:get()
    local new_speed = clamp(cur_speed + net_speed_inc * speed_inc, SPEED_MIN, max_speed)
    gat_spd_p:set(new_speed)
    gcs:send_text(6, string.format("transect: change GAT_SPD to %.2f m/s", new_speed))
  end

  -- Increment or decrement rf_target
  local net_rf_inc = sub:get_and_clear_button_count(BTN_INC_RF_TARGET) -
    sub:get_and_clear_button_count(BTN_DEC_RF_TARGET)
  if net_rf_inc ~= 0 and rf_target ~= nil then
    local rf_inc = math.max(0.01, gat_rf_inc_p:get())
    set_rf_target(rf_target + net_rf_inc * rf_inc)
    set_surftrak_target_cm(rf_target * 100.0)
  end
end

local function reset_guided_controller()
  guided_active = false
  commanded_speed = 0
  rf_lost = false
  last_rf_lost_warn_ms = 0
  last_prereq_warn_ms = 0
  last_valid_rf = nil
  -- Clear targets and ensure AC_PosControl offset is zeroed
  pos_target:x(0)
  pos_target:y(0)
  pos_target:z(0)
  vel_target:x(0)
  vel_target:y(0)
  vel_target:z(0)
  acc_target:x(0)
  acc_target:y(0)
  acc_target:z(0)
  poscontrol:set_posvelaccel_offset(ZERO_VEC, ZERO_VEC, ZERO_VEC)
end

local function update_guided_mode(rf_reading, dt)
  local now_ms = millis()

  if not guided_active then
    local current_pos = ahrs:get_relative_position_NED_origin()
    local current_vel = ahrs:get_velocity_NED()
    local send_prereq_warn = (prev_mode ~= GUIDED_MODE_NUM) or ((now_ms - last_prereq_warn_ms):tofloat() > 4000)

    -- Check prerequisites
    if not current_pos or not current_vel then
      if send_prereq_warn then
        last_prereq_warn_ms = now_ms
        gcs:send_text(4, "transect: waiting for EKF relative position")
      end
      return
    end

    if rf_reading == nil then
      if send_prereq_warn then
        last_prereq_warn_ms = now_ms
        gcs:send_text(4, "transect: waiting for rangefinder reading")
      end
      return
    end

    if above_surftrak_depth() then
      if send_prereq_warn then
        last_prereq_warn_ms = now_ms
        gcs:send_text(4, "transect: dive below SURFTRAK depth to engage script")
      end
      return
    end

    -- Set sticky target if not set yet
    if rf_target == nil then
      set_rf_target(rf_reading)
    end

    -- Initialize PVA controller targets
    yaw_target_rad = ahrs:get_yaw_rad()

    pos_target:x(current_pos:x())
    pos_target:y(current_pos:y())
    pos_target:z(current_pos:z())

    vel_target:x(current_vel:x())
    vel_target:y(current_vel:y())
    vel_target:z(current_vel:z())

    acc_target:x(0)
    acc_target:y(0)
    acc_target:z(0)

    -- Ensure any previous offset in poscontrol is cleared
    poscontrol:set_posvelaccel_offset(ZERO_VEC, ZERO_VEC, ZERO_VEC)

    guided_active = true
    last_rf_time_ms = now_ms
    last_valid_rf = rf_reading
    rf_lost = false
    local cruise_speed = clamp(gat_spd_p:get(), SPEED_MIN, wp_spd_p:get())
    gcs:send_text(6, string.format("transect: GUIDED active, target %.2fm, speed %.2fm/s", rf_target, cruise_speed))
  end

  if rf_reading ~= nil then
    last_rf_time_ms = now_ms
    last_valid_rf = rf_reading
    if rf_lost then
      rf_lost = false
      gcs:send_text(6, "transect: rangefinder recovered, forward resumed")
    end
  end

  local rf_dropout = false
  if (now_ms - last_rf_time_ms):tofloat() > 1000 then
    rf_dropout = true
    if not rf_lost or (now_ms - last_rf_lost_warn_ms):tofloat() > 4000 then
      last_rf_lost_warn_ms = now_ms
      gcs:send_text(4, "transect: rangefinder lost, forward stopped")
    end
    rf_lost = true
  end

  -- Pilot yaw steering: inspect yaw channel input
  local yaw_chan = rc:get_channel(rcmap_yaw_p:get())
  local yaw_input = yaw_chan and yaw_chan:norm_input_dz() or 0

  local gyro = ahrs:get_gyro()
  local yaw_rate_rads = gyro and gyro:z() or 0

  -- If the yaw stick is deflected or sub is actively turning, follow heading
  if yaw_input ~= 0 or math.abs(yaw_rate_rads) >= 0.05 then
    yaw_target_rad = ahrs:get_yaw_rad()
  end

  -- Desired forward speed (clamped to WP_SPD)
  local base_speed = clamp(gat_spd_p:get(), SPEED_MIN, wp_spd_p:get())

  -- Floor clearance & collision prevention (Task 1.2)
  local clr_min = gat_clr_min_p:get()
  local clr_slow = gat_clr_slow_p:get()

  -- Ensure slow threshold is at least clr_min
  if clr_slow < clr_min then
    clr_slow = clr_min
  end

  -- Guard: ensure slow threshold does not exceed rf_target - 0.05m
  if rf_target and rf_target > clr_min and clr_slow > (rf_target - 0.05) then
    clr_slow = math.max(clr_min, rf_target - 0.05)
  end

  local speed_scale = 1.0
  if rf_dropout then
    speed_scale = 0.0
  else
    local rf_check = rf_reading or last_valid_rf
    if rf_check ~= nil then
      if rf_check <= clr_min then
        speed_scale = 0.0
        if (now_ms - last_low_clr_warn_ms):tofloat() > 4000 then
          last_low_clr_warn_ms = now_ms
          gcs:send_text(4, string.format("transect: low clearance (%.2fm), forward stopped", rf_check))
        end
      elseif clr_slow > clr_min and rf_check < clr_slow then
        speed_scale = (rf_check - clr_min) / (clr_slow - clr_min)
      end
    end
  end

  local speed = base_speed * speed_scale
  commanded_speed = speed

  -- Desired horizontal velocity in NE frame (scalar math avoids 20 Hz heap allocations)
  local vel_desired_x = math.cos(yaw_target_rad) * speed
  local vel_desired_y = math.sin(yaw_target_rad) * speed

  -- Ramp horizontal velocity according to ACC_XY
  local diff_x = vel_desired_x - vel_target:x()
  local diff_y = vel_desired_y - vel_target:y()
  local diff_len = math.sqrt(diff_x * diff_x + diff_y * diff_y)

  local step_max = ACC_XY * dt
  if diff_len > step_max and diff_len > 1.0e-6 then
    local scale = step_max / diff_len
    diff_x = diff_x * scale
    diff_y = diff_y * scale
  end

  local vel_target_old_x = vel_target:x()
  local vel_target_old_y = vel_target:y()
  local vel_target_new_x = vel_target_old_x + diff_x
  local vel_target_new_y = vel_target_old_y + diff_y

  -- Update horizontal targets
  vel_target:x(vel_target_new_x)
  vel_target:y(vel_target_new_y)

  acc_target:x(diff_x / dt)
  acc_target:y(diff_y / dt)

  pos_target:x(pos_target:x() + (vel_target_old_x + vel_target_new_x) * 0.5 * dt)
  pos_target:y(pos_target:y() + (vel_target_old_y + vel_target_new_y) * 0.5 * dt)

  -- Vertical terrain following (bounded to autopilot limits & smoothed with ACC_Z)
  local rf_current = rf_dropout and rf_target or (rf_reading or last_valid_rf or rf_target)
  local rf_error = rf_target - rf_current

  local max_climb_speed = (wp_spd_up_p and wp_spd_up_p:get()) or 0.5
  local max_descend_speed = (wp_spd_dn_p and wp_spd_dn_p:get()) or 0.5

  -- In NED frame, negative Z is upwards (shallower depth / climb).
  -- If rf_current < rf_target (too close to bottom) -> rf_error > 0 -> vel_desired_z < 0 (climb)
  local vel_desired_z = clamp(-1.0 * (rf_error * P_GAIN_Z), -max_climb_speed, max_descend_speed)
  local vel_target_old_z = vel_target:z()

  -- Ramp vertical velocity according to WP_ACC_Z
  local max_acc_z = (wp_acc_z_p and wp_acc_z_p:get()) or ACC_Z
  if max_acc_z <= 0 then
    max_acc_z = ACC_Z
  end

  local diff_z = vel_desired_z - vel_target_old_z
  local step_max_z = max_acc_z * dt
  if math.abs(diff_z) > step_max_z then
    diff_z = (diff_z > 0) and step_max_z or -step_max_z
  end
  local vel_target_new_z = vel_target_old_z + diff_z

  vel_target:z(vel_target_new_z)
  acc_target:z(diff_z / dt)
  pos_target:z(pos_target:z() + (vel_target_old_z + vel_target_new_z) * 0.5 * dt)

  -- Enforce SURFTRAK_DEPTH minimum depth ceiling
  local min_depth = math.abs(surftrak_depth_p:get()) * 0.01
  if pos_target:z() < min_depth then
    pos_target:z(min_depth)
    if vel_target:z() < 0 then
      vel_target:z(0)
      acc_target:z(0)
    end
  end

  -- Send targets to vehicle position controllers
  vehicle:set_target_posvelaccel_NED(pos_target, vel_target, acc_target, false, 0, false, 0, false)

  send_rf_target_telemetry(false)
end

local function update_surftrak_mode(rf_reading)
  if above_surftrak_depth() then
    surftrak_active = false
    return
  end

  -- Get current SURFTRAK target
  local sub_target_cm = sub:get_rangefinder_target_cm()

  if rf_target == nil then
    if sub_target_cm and sub_target_cm > 0 then
      set_rf_target(sub_target_cm * 0.01)
    elseif rf_reading then
      set_rf_target(rf_reading)
    end
    if rf_target then
      set_surftrak_target_cm(rf_target * 100.0)
      surftrak_active = true
      gcs:send_text(6, string.format("transect: SURFTRAK active, target %.2fm", rf_target))
    end
  else
    -- If we just switched into SURFTRAK from another mode (e.g. GUIDED)
    -- or descended below SURFTRAK depth, push our sticky target to ArduSub.
    if not surftrak_active then
      local target_cm = rf_target * 100.0
      if sub_target_cm == nil or math.abs(target_cm - sub_target_cm) > 0.5 then
        set_surftrak_target_cm(target_cm)
      end
      surftrak_active = true
      gcs:send_text(6, string.format("transect: SURFTRAK active, target %.2fm", rf_target))
    elseif sub_target_cm and sub_target_cm > 0 then
      -- In steady SURFTRAK, if pilot changed the target via throttle stick,
      -- sync our sticky target so it carries over to GUIDED mode.
      local rf_inc = math.max(0.01, gat_rf_inc_p:get())
      local current_target = round(sub_target_cm * 0.01, rf_inc)
      if math.abs(current_target - rf_target) >= (rf_inc * 0.9) then
        set_rf_target(current_target)
        set_surftrak_target_cm(rf_target * 100.0)
      end
    end
  end
end

local function log_transect_state(current_mode, rf_reading)
  local current_pos = ahrs:get_relative_position_NED_origin()
  local depth = current_pos and current_pos:z() or 0
  local surftrak_target_cm = sub:get_rangefinder_target_cm()
  local surftrak_target = (surftrak_target_cm and surftrak_target_cm > 0) and (surftrak_target_cm * 0.01) or -1
  local speed = guided_active and commanded_speed or gat_spd_p:get()
  local target_z = guided_active and pos_target:z() or -1

  logger:write("TRNS",
    "Mode,RFTarg,RFRead,SurfTarg,Depth,TargZ,TargHead,Head,Spd",
    "Bffffffff",
    current_mode,
    rf_target or -1,
    rf_reading or -1,
    surftrak_target,
    depth,
    target_z,
    yaw_target_rad or 0,
    ahrs:get_yaw_rad() or 0,
    speed
  )
end

local function update()
  -- Calculate loop delta time
  local now_ms = millis()
  local dt = (now_ms - last_time_ms):tofloat() / 1000.0
  if dt <= 0 then
    return update, 1000 / RUN_HZ
  end
  if dt > MAX_DT then
    dt = MAX_DT
  end
  last_time_ms = now_ms

  respond_to_joystick_buttons()

  -- Handle disarmed state
  if not arming:is_armed() then
    if rf_target ~= nil then
      rf_target = nil
      gcs:send_text(6, "transect: forget rangefinder target")
    end
    if guided_active then
      reset_guided_controller()
    end
    last_valid_rf = nil
    surftrak_active = false
    return update, 1000 / RUN_HZ
  end

  -- Read rangefinder
  local rf_reading
  if sub:rangefinder_alt_ok() then
    rf_reading = rangefinder:distance_orient(ROTATION_PITCH_270)
  end

  local current_mode = vehicle:get_mode()

  if current_mode == GUIDED_MODE_NUM then
    surftrak_active = false
    update_guided_mode(rf_reading, dt)
  else
    if guided_active then
      reset_guided_controller()
    end

    if current_mode == SURFTRAK_MODE_NUM then
      update_surftrak_mode(rf_reading)
    else
      surftrak_active = false
      send_rf_target_telemetry(false)
    end
  end

  log_transect_state(current_mode, rf_reading)

  prev_mode = current_mode
  return update, 1000 / RUN_HZ
end

gcs:send_text(6, "transect4.lua loaded")
return update, 1000
