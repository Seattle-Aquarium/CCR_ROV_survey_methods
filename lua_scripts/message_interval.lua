--[[
message_interval.lua: set MAVLink message intervals for the MAVLink messages that we need

There is no way to query the current message interval, so just set it to the desired value every 5 seconds.
]]--

-- MAVLink messages that we need
local EKF_STATUS_REPORT      = uint32_t(193)
local GLOBAL_POSITION_INT    = uint32_t(33)
local GPS_RAW_INT            = uint32_t(24)
local LOCAL_POSITION_NED     = uint32_t(32)
local RANGEFINDER_MSG        = uint32_t(173)

-- Desired message frequencies in Hz
local intervals = {
  {EKF_STATUS_REPORT, 3},     
  {GLOBAL_POSITION_INT, 3},   
  {GPS_RAW_INT, 3},           
  {LOCAL_POSITION_NED, 3},    
  {RANGEFINDER_MSG, 3},       
}

function update()
  for _, msg in ipairs(intervals) do
    gcs:set_message_interval(0, msg[1], math.floor(1000000 / msg[2]))
  end
  return update, 5000 -- check and update every 5 seconds
end

gcs:send_text(6, "message_interval.lua: loaded")
return update()