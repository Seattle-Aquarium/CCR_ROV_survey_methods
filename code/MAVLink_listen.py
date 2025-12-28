#!/usr/bin/env python3

"""
Listen for MAVLink messages and list the message types and sources.

Connect to udpin:0.0.0.0:14550 (this should work for most situations):
    MAVLink_listen.py

Connect to a different IP address or port:
    MAVLink_listen.py --connection udpin:0.0.0.0:14551

Call out requests to get messages (MAV_CMD_SET_MESSAGE_INTERVAL, MAV_CMD_REQUEST_MESSAGE):
    MAVLink_listen.py --requests
"""

import time
import threading
from pymavlink import mavutil


import argparse

def mavlink_server(connection_string, show_requests=False):
    print(f"Waiting for connection on {connection_string}...")
    master = mavutil.mavlink_connection(connection_string)

    # Count messages by (msg_type, src_id, comp_id) tuple
    msg_counts: dict[tuple[str, int, int], int] = {}

    # Send HEARTBEAT at 1Hz
    def send_heartbeat():
        while True:
            # Type: GCS (6), Autopilot: Generic (0)
            master.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS,
                mavutil.mavlink.MAV_AUTOPILOT_GENERIC,
                0, 0, 0
            )
            time.sleep(1)

    # Start HEARTBEAT sender in a daemon thread so it dies when the main script stops
    heartbeat_thread = threading.Thread(target=send_heartbeat, daemon=True)
    heartbeat_thread.start()

    print("MAVLink server started. Press Ctrl+C to stop and dump stats.\n")
    print("--- MAVLink Message Types and Sources ---")
    print(f"{'Message Type':<30} | {'SysID':<5} | {'CompID':<6}")
    print("-" * 47)

    start_time = time.time()
    try:
        while True:
            msg = master.recv_match(blocking=True)
            if not msg:
                continue

            msg_type = msg.get_type()
            src_id = msg.get_srcSystem()
            comp_id = msg.get_srcComponent()
            signature = (msg_type, src_id, comp_id)

            if signature not in msg_counts:
                print(f"{msg_type:30} | {src_id:<5} | {comp_id:<6}")
                msg_counts[signature] = 1
            else:
                msg_counts[signature] += 1

            if show_requests and msg_type in ['COMMAND_LONG', 'COMMAND_INT']:
                try:
                    cmd_id = msg.command
                    
                    if cmd_id in [mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE]:
                        requested_msg_id = int(msg.param1)
                        
                        # Resolve message name
                        try:
                            msg_class = mavutil.mavlink.mavlink_map[requested_msg_id]
                            # Try .msgname first (newer pymavlink), fall back to .name if needed, or class name
                            if hasattr(msg_class, 'msgname'):
                                requested_msg_name = msg_class.msgname
                            elif hasattr(msg_class, 'name'):
                                requested_msg_name = msg_class.name
                            else:
                                requested_msg_name = f"MSG_ID_{requested_msg_id}"
                        except KeyError:
                            requested_msg_name = f"UNKNOWN_{requested_msg_id}"
                            
                        req_type = "Request"
                        rate_str = ""
                        
                        if cmd_id == mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL:
                            interval_us = msg.param2
                            req_type = "Set Interval"
                            if interval_us == -1:
                                rate_str = " (Disabled)"
                            elif interval_us == 0:
                                rate_str = " (Default)"
                            elif interval_us > 0:
                                rate_hz = 1000000.0 / interval_us
                                rate_str = f" ({rate_hz:.1f} Hz)"
                        elif cmd_id == mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE:
                            req_type = "One-shot"
                        
                        print(f"Request: {req_type} by [{src_id}:{comp_id}] for {requested_msg_name}{rate_str}")
                        
                except Exception as e:
                    pass

    except KeyboardInterrupt:
        duration = time.time() - start_time
        print("\n\n--- MAVLink Message Statistics ---")
        print(f"{'Message Type':<30} | {'SysID':<5} | {'CompID':<6} | {'Count':<8} | {'Rate (Hz)'}")
        print("-" * 70)
        
        # Sort by message type
        sorted_stats = sorted(msg_counts.items(), key=lambda item: item[0][0])
        
        for (msg_type, src_id, comp_id), count in sorted_stats:
            rate = count / duration if duration > 0 else 0.0
            print(f"{msg_type:<30} | {src_id:<5} | {comp_id:<6} | {count:<8} | {rate:.2f}")
        
        print(f"\nTotal unique signatures: {len(msg_counts)}")
        print(f"Total duration: {duration:.2f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
    parser.add_argument("--requests", action="store_true", help="Report on MAV_CMD_SET_MESSAGE_INTERVAL and MAV_CMD_REQUEST_MESSAGE")
    parser.add_argument("--connection", default="udpin:0.0.0.0:14550", help="Connection string for MAVLink messages")
    args = parser.parse_args()

    mavlink_server(args.connection, show_requests=args.requests)