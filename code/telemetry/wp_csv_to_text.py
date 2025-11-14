#!/usr/bin/env python3

"""
Read a csv file with Latitude and Longitude, and write a QGC-compatible text file with waypoints.

Usage:
wp_csv_to_text.py example_coords.csv > example_wp.txt

Input:
Latitude,Longitude
47.630268,-122.3982391
47.630268,-122.3982428
47.63026656,-122.3982551
...

Output:
QGC WPL 110
0	0	3	16	0	0	0	0	47.630268	-122.3982391	-1.0	1
1	0	3	16	0	0	0	0	47.630268	-122.3982428	-1.0	1
2	0	3	16	0	0	0	0	47.63026656	-122.3982551	-1.0	1
...

Reference: https://mavlink.io/en/file_formats/
"""

import argparse
import csv


def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
    parser.add_argument('--altitude', type=float, default=-1.0, help='altitude in meters, default -1.0')
    parser.add_argument('--frame', type=int, default=3, help='frame type, default 3 (global)')
    parser.add_argument('path')
    args = parser.parse_args()

    with open(args.path, newline='') as csvfile:
        reader = csv.reader(csvfile, delimiter=',', quotechar='|')

        # Ignore csv header
        next(reader)

        # Write txt header
        print('QGC WPL 110')

        for i, row in enumerate(reader):
            # if i == 0:
                # The first row is the home position, at altitude 0
                # print(f'{i}\t0\t{args.frame}\t16\t0\t0\t0\t0\t{row[0]}\t{row[1]}\t0\t1')
            # else:
                # All other rows are waypoints with the appropriate frame and altitude
            print(f'{i}\t0\t{args.frame}\t16\t0\t0\t0\t0\t{row[0]}\t{row[1]}\t{args.altitude}\t1')


if __name__ == '__main__':
    main()
