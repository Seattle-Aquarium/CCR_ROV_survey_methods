import os
import datetime
import exifread

# Function to get EXIF DateTimeOriginal from image files (.GPR and .JPG)
def get_original_timestamp(image_path):
    with open(image_path, 'rb') as f:
        tags = exifread.process_file(f)
        original_time_tag = 'EXIF DateTimeOriginal'
        if original_time_tag in tags:
            return str(tags[original_time_tag])
    return None

# Function to rename .GPR and .JPG files
def rename_files(directory):
    for filename in os.listdir(directory):
        if filename.lower().endswith(('.gpr', '.jpg')):
            file_path = os.path.join(directory, filename)
            original_timestamp = get_original_timestamp(file_path)
            
            if original_timestamp:
                # Format the timestamp as YYYY_MM_DD_HH-MM-SS
                timestamp = datetime.datetime.strptime(original_timestamp, "%Y:%m:%d %H:%M:%S").strftime('%Y_%m_%d_%H-%M-%S')
                
                # Preserve the file extension (.GPR or .JPG)
                file_extension = os.path.splitext(filename)[1].upper()
                new_filename = f"{timestamp}{file_extension}"
                new_file_path = os.path.join(directory, new_filename)

                # Handle filename conflicts by appending a counter
                counter = 1
                while os.path.exists(new_file_path):
                    new_filename = f"{timestamp}_{counter}{file_extension}"
                    new_file_path = os.path.join(directory, new_filename)
                    counter += 1
                
                # Rename the file
                os.rename(file_path, new_file_path)
                print(f"Renamed '{filename}' to '{new_filename}'")
            else:
                print(f"Skipping '{filename}'. No original timestamp found in EXIF data.")

# Command-line input
if __name__ == "__main__":
    # Ask user for input folder path
    directory = input("Please enter the jpg/gpr image folder path: ")
    
    # Call the function to rename files
    rename_files(directory)
