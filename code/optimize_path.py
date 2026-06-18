"""
Smooth the ROV path using a moving average, and generate a list of meter marks.

There are two modes:

Analysis: given a known path length, find the optimal smoothing window size.
Usage: python optimize_path.py --path-length <length> <logfile>

Generation: given a window size, generate a list of meter marks.
Usage: python optimize_path.py --window-size <size> <logfile>

In both modes you can add options:

    --csv: save the optimized path to a CSV file
    --plot: create a plot comparing the original and optimized paths
    --move-images: moves images to matching/closest timestamps
        --gpr-folder: origin directory to move images from
        --dest_folder: destination directory to move images into
"""

import argparse
import math
import os

import matplotlib.pyplot as plt
import pandas as pd

import meter_mark


def moving_average_smoothing(positions, window_size):
    """
    Apply a moving average smoothing to the positions.

    positions: list of (timestamp, x, y) tuples
    window_size: odd integer
    """
    if window_size < 1:
        return positions
    if window_size == 1:
        return positions

    # Extract x and y
    timestamps = [p[0] for p in positions]
    xs = [p[1] for p in positions]
    ys = [p[2] for p in positions]

    df = pd.DataFrame({'t': timestamps, 'x': xs, 'y': ys})

    # We want a centered window, so we'll center=True
    # min_periods=1 ensures we get values at the edges too
    smoothed = df.rolling(
        window=window_size, center=True, min_periods=1
    ).mean()

    # Reconstruct list of tuples
    new_positions = []
    for t, x, y in zip(df['t'], smoothed['x'], smoothed['y']):
        new_positions.append((t, x, y))

    return new_positions


def subsample_positions(positions, n):
    """Take every n-th position."""
    return positions[::n]


def calculate_path_length(positions):
    """Calculate the cumulative distance of the path."""
    dist = 0.0
    for i in range(1, len(positions)):
        p1 = positions[i-1]
        p2 = positions[i]
        d = math.sqrt((p2[1]-p1[1])**2 + (p2[2]-p1[2])**2)
        dist += d
    return dist


def calculate_rmse(original_pos, smoothed_pos):
    """
    Calculate Root Mean Square Error between original and smoothed paths.

    Assumes lists are same length (which they are for moving average with
    current impl).
    """
    if len(original_pos) != len(smoothed_pos):
        # If lengths differ (e.g. subsampling), we can't easily do
        # point-to-point RMSE without interpolation.
        return -1.0

    sq_errors = []
    for p1, p2 in zip(original_pos, smoothed_pos):
        d2 = (p1[1]-p2[1])**2 + (p1[2]-p2[2])**2
        sq_errors.append(d2)

    return math.sqrt(sum(sq_errors) / len(sq_errors))


def create_plot(original_path, optimized_path, meter_records, output_filename):
    """
    Create a Matplotlib plot comparing paths and save as PDF.

    original_path: list of (t, x, y)
    optimized_path: list of (t, x, y)
    meter_records: list of dicts with 'x', 'y' (and 'meter_number' etc.)
    output_filename: path to save the PDF
    """
    if not original_path or not optimized_path:
        print('No data to plot.')
        return

    # Extract X and Y for original path
    orig_x = [p[1] for p in original_path]
    orig_y = [p[2] for p in original_path]

    # Extract X and Y for optimized path
    opt_x = [p[1] for p in optimized_path]
    opt_y = [p[2] for p in optimized_path]

    # Extract X, Y, and label for meter markers
    meter_x = [m['x'] for m in meter_records]
    meter_y = [m['y'] for m in meter_records]
    meter_labels = [m['meter_number'] for m in meter_records]

    plt.figure(figsize=(10, 10))

    # Plot East on X, North on Y
    plt.plot(
        orig_y, orig_x, label='Original path',
        color='red', alpha=0.5, linewidth=0.5
    )
    plt.plot(
        opt_y, opt_x, label='Optimized path',
        color='blue', alpha=0.8, linewidth=0.5
    )

    # Markers
    plt.scatter(
        meter_y, meter_x, c='green', s=2, zorder=5, label='Meter markers'
    )

    # Annotations
    for x, y, label in zip(meter_x, meter_y, meter_labels):
        if label % 10 == 0 or label == 0 or label == meter_labels[-1]:
            plt.annotate(
                str(label), (y, x), textcoords="offset points",
                xytext=(3, 3), fontsize=8
            )

    plt.axis('equal')
    plt.xlabel('East (m)')
    plt.ylabel('North (m)')
    plt.title('ROV Path Optimization')
    plt.legend()
    plt.grid(True, alpha=0.3)

    plt.savefig(output_filename, format='pdf')
    plt.close()
    print(f'Plot saved to: {output_filename}')


def optimize_path(
    logfile, known_length=None, window_size=None,
    save_csv=False, plot=False, move_images=False, gpr_folder=False, dest_folder=False # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
):
    """Find window size, and apply to generate meter markers."""
    original_positions = meter_mark.process_tlog(logfile)

    if not original_positions:
        print('No positions found.')
        return

    original_len = calculate_path_length(original_positions)
    print(f'Original length:    {original_len:.4f} m')

    best_result = None

    if known_length is not None:
        print(f'Known length:       {known_length:.4f} m')
        results = []
        best_ma_diff = float('inf')

        for w in range(1, 202, 2):  # Test windows 1 to 201
            smoothed = moving_average_smoothing(original_positions, w)
            length = calculate_path_length(smoothed)
            diff = abs(length - known_length)
            rmse = calculate_rmse(original_positions, smoothed)

            results.append({
                'method': 'MovAvg',
                'param': w,
                'length': length,
                'diff': length - known_length,
                'rmse': rmse,
                'positions': smoothed
            })

            if diff < best_ma_diff:
                best_ma_diff = diff

        # Sort results by absolute difference, but prefer length >= target
        # if very close. Let's filter for valid candidates that reach the
        # target if possible.
        valid_candidates = [
            r for r in results if r['length'] >= known_length
        ]

        if valid_candidates:
            # Pick the one closest to target from the valid ones
            valid_candidates.sort(key=lambda x: x['length'])
            best_result = valid_candidates[0]
        else:
            # Fallback to closest overall
            best_result = results[0]

        print(f"Best window size:   {best_result['param']}")
    elif window_size is not None:
        print(f'Given window size:  {window_size}')
        smoothed = moving_average_smoothing(original_positions, window_size)
        length = calculate_path_length(smoothed)
        rmse = calculate_rmse(original_positions, smoothed)

        best_result = {
            'method': 'MovAvg',
            'param': window_size,
            'length': length,
            'rmse': rmse,
            'positions': smoothed
        }

    else:
        print("Error: Must provide either --path-length or --window-size.")
        return

    print(f"Optimized length:   {best_result['length']:.4f} m")
    print(f"RMSE from original: {best_result['rmse']:.4f} m")

    best_positions = best_result['positions']
    meter_records = meter_mark.positions_to_meter_records(best_positions)

    # Add the 0th meter record manually if it doesn't exist
    p0 = best_positions[0]
    import datetime
    pacific = meter_mark.pytz.timezone('US/Pacific')
    t0 = datetime.datetime.fromtimestamp(
        p0[0], tz=datetime.timezone.utc
    ).astimezone(pacific)

    record0 = {
        'meter_number': 0,
        'timestamp': t0.timestamp(),
        'strftime': t0.strftime('%Y_%m_%d_%H-%M-%S'),
        'cumulative_dist': 0.0,
        'step_distance_m': 0.0,
        'gap_seconds': 0.0,
        'quality_flag': 'good',
        'x': p0[1],
        'y': p0[2]
    }
    meter_records.insert(0, record0)

    # Save if requested and valid
    if save_csv:
        df = pd.DataFrame(
            meter_records,
            columns=[
                'meter_number',
                'timestamp',
                'strftime',
                'cumulative_dist',
                'step_distance_m',
                'gap_seconds',
                'quality_flag',
                'x',
                'y'
            ]
        )

        if not df.empty:
            filename = os.path.splitext(
                os.path.basename(logfile)
            )[0] + '_optimized_meter_markers.csv'
            # save to same dir as logfile
            save_dir = os.path.dirname(logfile)
            csv_path = os.path.join(save_dir, filename)
            df.to_csv(csv_path, index=False)
            print(f'Total markers:      {len(df)}')
            print(f'Meter markers saved to: {csv_path}')

    if plot:
        output_pdf = os.path.splitext(logfile)[0] + '_optimized_path.pdf'
        create_plot(
            original_positions, best_positions, meter_records, output_pdf
        )

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    if move_images:
        if not gpr_folder or not dest_folder:
            print("Error: --gpr-folder and --dest-folder are required when using --move-images.")
            return
        if meter_records:
            meter_mark.move_images_based_on_markers(
                meter_records, gpr_folder, dest_folder
            )
        else:
            print("No meter records available to move images.")
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__
    )
    parser.add_argument('logfile', help='Path to the .tlog file')
    parser.add_argument(
        '--csv', action='store_true',
        help='Save the optimized meter markers to CSV'
    )
    parser.add_argument(
        '--plot', action='store_true',
        help='Generate a PDF plot of the paths'
    )
    parser.add_argument(
        '--path-length', type=float,
        help='Analysis mode: known path length in meters'
    )
    parser.add_argument(
        '--window-size', type=int,
        help='Generation mode: window size for smoothing'
    )

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    parser.add_argument(
        '--move-images', action='store_true',
        help='Move images associated with the transect'
    )
    parser.add_argument(
        '--gpr-folder', type=str,
        help='Folder containing gpr images'
    )
    parser.add_argument(
        '--dest-folder', type=str,
        help='Destination folder for moved gpr images'
    )
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    args = parser.parse_args()

    if args.path_length is None and args.window_size is None:
        parser.error('Must provide either --path-length or --window-size')
    elif args.path_length is not None and args.window_size is not None:
        parser.error('Must provide either --path-length or --window-size')

    if args.path_length is not None:
        optimize_path(
            args.logfile, known_length=args.path_length,
            save_csv=args.csv, plot=args.plot, move_images=args.move_images, gpr_folder=args.gpr_folder, dest_folder=args.dest_folder # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        )
    else:
        optimize_path(
            args.logfile, window_size=args.window_size,
            save_csv=args.csv, plot=args.plot, move_images=args.move_images, gpr_folder=args.gpr_folder, dest_folder=args.dest_folder # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        )


if __name__ == '__main__':
    main()
