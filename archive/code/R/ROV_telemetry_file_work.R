##################### ~~~ ROV Telemetry File Work ~~~ #########################

#set working directory 
setwd(here::here())

# path to Ping files = ./ROV_telemetry/Ping
# path to telemetry files = ./ROV_telemetry/QGC

## Startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
library(tidyverse)
library(sf)
library(dplyr)
library(rgdal)
library(sp)
library(hms)
library(measurements)

rm(list = ls())
## END startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##



########### ~~~~~~~~~~ GPS tracklog cleanup ~~~~~~~~~~~~~~~~~~~~~~~ ############

# load main telemetry file 
main <- read.csv("./ROV_telemetry/QGC/2022-08-15 09-56-00 vehicle1.csv")

## Process columns ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
# filter columns
filtered <- main[c("heading", "altitudeRelative", "flightTime", 
                  "battery0.percentRemaining", "clock.currentTime",
                  "clock.currentDate","gps.lat","gps.lon","gps.count",
                  "temperature.temperature2")]


# rename columns
renamed <- rename(filtered, 
                  depth=altitudeRelative, 
                  flight_time=flightTime,
                  battery_remaining=battery0.percentRemaining,
                  time=clock.currentTime,
                  date=clock.currentDate,
                  lat=gps.lat,
                  lon=gps.lon,
                  satellite_count=gps.count,
                  temp=temperature.temperature2)


# reorder columns
ordered <- renamed[, c(6, 5, 3, 2, 1, 4, 7, 8, 9, 10)]


# create sequence of minutes to plot along x-axis
ordered$seq <- seq(1, nrow(ordered), by=1) / 60


# function to create a running dive clock column 
x.max <- function(df){
  s1 <- nrow(df)/60
  s2 <- round(s1, digits=0)
  return(s2)
}

# call function
xlim <- x.max(ordered)


# Convert depth data from ft to m 
ordered$depth <- conv_unit(ordered$depth, "ft", "m")


# tidy up
main.clean <- ordered
remove(renamed, filtered, ordered, main)

### ~~~~~~~~~~~~~~~~~ END GPS tracklog cleanup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ###




########## ~~~~~~ Calculate Euclidean Distance between GPS points ~~~~ ##########

## Euclidean Distance ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
# Convert coordinates to SpatialPoint object 
cord.dec = SpatialPoints(cbind(main.clean$lon, -main.clean$lat), proj4string = CRS("+proj=longlat"))


# Transform coordinate system to UTM using EPSG:32610 - WGS 84 / UTM zone 10N
cord.UTM <- spTransform(cord.dec, CRS("+init=epsg:32610"))


# Calculate Euclidean distance (m) from UTM coordinates 
euclidDist <- sp::spDists(cord.UTM, longlat = FALSE, segments=TRUE)


# Append Euclidean distance values to data frame with first row as NA
main.clean$EucDIS <- c(NA, euclidDist)


# tidy up
main.clean.dis <- main.clean
remove(cord.dec, cord.UTM, main.clean)

### ~~~~~~~~~~~~~ END distance calculation ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ###



########## ~~~ Average Altimeter and Confidence Interval from Ping ~~~ ##########
# CAUTION: Make sure the Ping bin file is converted to a csv file using the python 
# script (Ping_to_csv.py) before proceeding 


# load the appropriate Ping csv file (same time as the main telemetry file)
ping <- read.csv("./ROV_telemetry/Ping/20220815-095728695.csv")


## Calculate average altimeter and confidence interval per second ~~~~~~~~~~~ ##
# rename columns 
names(ping)[2] <- "dist"
names(ping)[3] <- "conf"


# Convert altimeter data from mm to m 
ping$dist <- conv_unit(ping$dist, "mm", "m")


# function to calculate average ping and confidence per 1s 
Avg.Ping <- function(x){
  
  x$time <- as_hms(ymd_hms(x$timestamp)) ## extract HH:MM:SS
  x$short <- str_sub(x$time, start=1L, end=8L) ## delete the fractions of a second characters
  
  sec <- aggregate(x$dist, by=list(time=x$short), FUN=mean) ## calculate average based on second
  names(sec)[2] <- "avg_dist" ## rename column 
  
  conf <- aggregate(x$conf, by=list(time=x$short), FUN=mean) ## calculate average based on second 
  names(conf)[2] <- "avg_conf" ## rename column 
  
  out <- cbind(sec, conf$avg_conf) ## bind the relevant columns
  names(out)[3] <- "avg_conf" ## rename colume 
  
  return(out) ## export output
  
}


# call function
new_ping <- Avg.Ping(ping)


# tidy up
remove(ping)

## END Avg Altimeter and Conf Interval ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##



########## ~~~~~~~~~~~  Merge Telemetry and Ping Data  ~~~~~~~~~~  ##########

## Merge cleaned main telemetry data and average ping dataframes together using time 
# and drop rows with NA values ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
dat <- merge(main.clean.dis, new_ping, by.x = "time", all=FALSE)


# create csv file with telemetry and Ping data into 'Cleaned' folder (./ROV_telemetry/Cleaned)
write.csv(dat, "./ROV_telemetry/Cleaned/2022-08-15_09-57-40copy.csv", row.names = FALSE)

## END merge data frames  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##


## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
