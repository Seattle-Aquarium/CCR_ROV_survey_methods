########## ~~~~~~~~ Merge telemetry and CoralNet % Cover ~~~~~~~~~~~~ ##########
## minor edit to see if I can push test; test again

library(dplyr)
library(tidyr)
library(lubridate)
rm(list = ls())

# set working directory 
setwd('/Users/path....') 


## Calculate flight time in cumulative seconds ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##

# read in telemetry csv file for a transect and remove NA value
tel <- read.csv('2022-08-15_10-01-31.csv')
tel <- na.omit(tel)

# convert flight time character string to Period class
time <- hms(tel$flight_time)

# convert period class to seconds 
time_sec <- period_to_seconds(time)

# append seconds vector to tel data frame 
tel$flight_sec <- time_sec

# create new column for amount of time passed (sec) between rows 
tel <- tel %>% mutate(sec_diff = flight_sec - lag(flight_sec))

# make NA value equal to 0 
tel <- tel %>% replace(is.na(.), 0)

# create new column of cumulative flight time in seconds
tel <- tel %>% mutate(sec_csum = cumsum(sec_diff))



## Create image name column ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##

# create new columns that will consist of the image name
tel <- tel %>% mutate(date_transect = "2022_08_15_",time_transect = "10-01-31_",
                       jpeg = ".jpg")

# unite columns for image name into new image name column that includes the cumulative time
tel <- tel %>% unite(col ="img_name", c(date_transect, time_transect,
                                         sec_csum, jpeg), sep = '', remove = FALSE)


# remove unnecessary columns: flight seconds sec_diff
tel <- select(tel, - c(flight_sec, sec_diff, date_transect, time_transect, add, jpeg))
tel <- tel %>% relocate(sec_csum, .before = depth)


## Merge Telemetry and CoralNet csvs based on image name ~~~~~~~~~~~~~~~~~~~~ ##
# read in CoralNet % cover data 
CN <- read.csv("percent_covers (6).csv")

# remove unnecessary CoralNet columns: Image ID, Annotation status, Points
CN <- select(CN, - c(Image.ID, Annotation.status, Points))

# rename image name column in CoralNet to match image name column of telemetry file
CN <- CN %>% rename(img_name = Image.name)

# merge telemetry and CoralNet dataframes based on image name 
tel_CN <- merge (tel, CN, by.x = "img_name")
tel_CN <- tel_CN %>% relocate(img_name, .before = cup_coral)

