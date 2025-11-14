from decode_sensor_binary_log import PingViewerLogReader
from datetime import datetime, timedelta, time
from pathlib import Path
import csv

# TODO: put in the path to the log file you want to process
#  e.g. "~/My Documents/PingViewer/Sensor_Log/something.bin"
logfile = Path(r"C:\Users\williamsm\Desktop\SEAQ\Python\Ping\20220815-115551033.bin")

def to_timedelta(time_str: str) -> timedelta:
    ''' Returns a time delta from an iso-format time string. '''
    delta = time.fromisoformat(time_str)
    return timedelta(hours=delta.hour, minutes=delta.minute,
                     seconds=delta.second, microseconds=delta.microsecond)

log = PingViewerLogReader(logfile)
outfile = Path(logfile.stem).with_suffix(".csv")
# ideally it would be good to localise this to your timezone,
#  but in this case it shouldn't cause issues unless you were
#  recording the sonar data at the time of a daylight savings
#  changeover or something
start_time = datetime.strptime(logfile.stem, "%Y%m%d-%H%M%S%f")

with outfile.open("w") as out:
    csv_writer = csv.writer(out)
    csv_writer.writerow(("timestamp", "distance [mm]", "confidence [%]"))
    for timestamp, message in log.parser():
        # convert the 'duration since scanning started' into a local-time timestamp
        #  the .replace here ensures cross-compatibility between operating systems
        timestamp = start_time + to_timedelta(timestamp.replace('\x00',''))
        csv_writer.writerow((timestamp, message.distance, message.confidence))