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
0 1 3 16 2 3 4 5 47.630268 -122.3982391 -1.0 1
1 6 3 16 7 8 9 10 47.630268 -122.3982428 -1.0 1
2 11 3 16 12 13 14 15 47.63026656 -122.3982551 -1.0 1
...

Reference: https://mavlink.io/en/file_formats/
"""

import argparse
import csv


def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
    parser.add_argument('--altitude', type=float, default=-1.0, help='altitude in meters, default -1.0')
    parser.add_argument('--frame', type=int, default=3, help='frame type, default 3 (global)')
    path = input("PathToInputFile:")
    args = parser.parse_args()
    result = input("PathToResultFile: ")
    path_save = open(result, "a")


    with open(path, newline='') as csvfile:
        reader = csv.reader(csvfile, delimiter=',', quotechar='|')

        # Ignore csv header
        next(reader)
        line_count = 0
        n = 0
        path_save.write("QGC WPL 110\n")
        for i, row in enumerate(reader):
            if line_count % 10 == 0:  # Check if it's the 10th line
                path_save.write(f'{n}\t0\t{args.frame}\t16\t0\t0\t0\t0\t{row[15]}\t{row[16]}\t{args.altitude}\t1\n')
                n += 1
            line_count += 1


if __name__ == '__main__':
    main()

