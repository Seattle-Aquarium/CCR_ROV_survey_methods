##################### ~~~ ROV Telemetry File Work ~~~ #########################

#set working directory 
setwd(here::here())

# path to files =  ./Imagery/(date)/telemetry

## Startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
library(tidyverse)
library(sf)
library(dplyr)
library(rgdal)
library(sp)
library(rgdal)
library(hms)
library(measurements)
library(lubridate)
library(leaflet)
library(trajr)
library(zoo)
library(geosphere) 

rm(list = ls())
## END startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##



########### ~~~~~~~~~~ GPS tracklog cleanup ~~~~~~~~~~~~~~~~~~~~~~~ ############

# load main telemetry file 
main <- read.csv("./dives/testing/2024_06_12_Diver_ROV/logs/2024-06-12 09-41-47 vehicle1.csv")
main2 <- read.csv("./Dives/2023_09_06/logs/2023-09-06 12-15-37 vehicle1.csv")
main3 <- read.csv("./Dives/2023_09_06/logs/2023-09-06 13-07-08 vehicle1.csv")
main4 <- read.csv("./Dives/2023_09_14/logs/2023-01-30 12-06-38 vehicle1.csv")


main <- rbind(main1, main2, main3)
## Process columns ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
# filter columns
filtered <- main[c("heading", "groundSpeed", "altitudeRelative", "flightTime", 
                   "clock.currentTime", "clock.currentDate", "gps.lat", "gps.lon", 
                   "localPosition.x","localPosition.y", "apmSubInfo.rangefinderDistance", 
                   "temperature.temperature2")]


# rename columns
renamed <- rename(filtered, 
                  depth=altitudeRelative, 
                  flight_time=flightTime,
                  time=clock.currentTime,
                  date=clock.currentDate,
                  lat=gps.lat,
                  lon=gps.lon,
                  DVLx=localPosition.x,
                  DVLy=localPosition.y,
                  altitude=apmSubInfo.rangefinderDistance,
                  temp=temperature.temperature2)


# reorder columns
ordered <- renamed[, c(5, 6, 4, 3, 1, 12, 2, 7, 8, 9, 10, 11)]


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
dat <- ordered
remove(filtered, ordered, renamed)


### ~~~~~~~~~~~~~~~~~ END QGC tracklog cleanup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ###





########## ~~~~~~~~~~~~~~~~~~  Create Transects  ~~~~~~~~~~~~~~~~~~~  ##########

### create function that creates a new df based on start and end times ------- ##
snip <- function(df, start, end){
  out <- df[df$time >= start & df$time <= end, ]
  return(out)
}

# call function -- enter the dataframe name, the start time, and the end time
T1 <- snip(dat, '10:01:40', '10:33:12')
T2 <- snip(dat, '14:30:25', '14:40:55')
T3 <- snip(dat, '14:57:22', '15:08:56')
T4 <- snip(dat, '15:16:27', '15:28:25')


T1$DVLx <- as.numeric(T1$DVLx)
T1$DVLy <- as.numeric(T1$DVLy)

T2$DVLx <- as.numeric(T2$DVLx)
T2$DVLy <- as.numeric(T2$DVLy)

T3$DVLx <- as.numeric(T3$DVLx)
T3$DVLy <- as.numeric(T3$DVLy)

T4$DVLx <- as.numeric(T4$DVLx)
T4$DVLy <- as.numeric(T4$DVLy)

## END snipping function ---------------------------------------------------- ##


#### ~~~~~~~~~~ Function to convert local coordinates to global coordinates~~~~##

# Function to convert DVL movements to latitude and longitude changes
dvl_to_latlon <- function(start_lat, start_lon, x_movements, y_movements, initial_heading) {
  # Convert initial heading to radians
  heading_rad <- -(initial_heading * pi / 180)
  
  # Initialize vectors to store new coordinates
  latitudes <- numeric(length(x_movements))
  longitudes <- numeric(length(x_movements))
  
  # Starting point
  current_lat <- start_lat
  current_lon <- start_lon
  
  # Set the first coordinates to the starting position
  latitudes[1] <- current_lat
  longitudes[1] <- current_lon
  
  for (i in 2:length(x_movements)) {
    # Net change in position
    net_y_change <- -(x_movements[i] - x_movements[i - 1])
    net_x_change <- -(y_movements[i] - y_movements[i - 1])
    
    # Rotate the net changes by the initial heading
    rotated_x_change <- net_x_change * cos(heading_rad) - net_y_change * sin(heading_rad)
    rotated_y_change <- net_x_change * sin(heading_rad) + net_y_change * cos(heading_rad)
    
    # Convert the net changes to meters (assuming they are in meters)
    distance <- sqrt(rotated_x_change^2 + rotated_y_change^2)
    
    # Calculate the change in latitude and longitude
    delta_lat <- rotated_y_change / 111320  # Latitude degrees per meter
    delta_lon <- rotated_x_change / (111320 * cos(current_lat * pi / 180))  # Longitude degrees per meter
    
    # Update the current coordinates
    current_lat <- current_lat + delta_lat
    current_lon <- current_lon + delta_lon
    
    # Store the new coordinates
    latitudes[i] <- current_lat
    longitudes[i] <- current_lon
  }
  
  return(data.frame(lat = latitudes, lon = longitudes))
}



# Load the CSV file with x, y movements and starting coordinates
dvl_data <- T1

# Extract the starting coordinates (these should be provided as initial inputs)
start_lat <- dvl_data$lat[1] 
start_lon <- dvl_data$lon[1] 
intial_heading <- dvl_data$head[1]

# Extract the movements
x_movements <- dvl_data$DVLx
y_movements <- dvl_data$DVLy


# Call the function
new_coordinates <- dvl_to_latlon(start_lat, start_lon, x_movements, y_movements, initial_heading)


# Add the new coordinates to the original data
dvl_data <- dvl_data %>%
  mutate(DVLlat = new_coordinates$lat,
         DVLlon = new_coordinates$lon)
         
# map check
leaflet() %>%
  addTiles() %>%
  addPolylines(data=dvl_data, lat=~DVLlat, lng=~DVLlon, weight=1, color="red", opacity=1) %>%
  addPolylines(data=dvl_data, lat=~lat, lng=~lon, weight=1, color="black", opacity=1)


###### ---------------------------



########## ~~~~~~ Calculate Euclidean Distance between RAW GPS points ~~~~ ##########

## Euclidean Distance ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
# Convert coordinates to SpatialPoint object 
cord.dec1 = SpatialPoints(cbind(T1_merged$DVLlon, -T1_merged$DVLlat), proj4string = CRS("+proj=longlat"))
cord.dec2 = SpatialPoints(cbind(T2_merged$DVLlon, -T2_merged$DVLlat), proj4string = CRS("+proj=longlat"))
cord.dec3 = SpatialPoints(cbind(T3_merged$DVLlon, -T3_merged$DVLlat), proj4string = CRS("+proj=longlat"))
cord.dec4 = SpatialPoints(cbind(T4_merged$DVLlon, -T4_merged$DVLlat), proj4string = CRS("+proj=longlat"))


# Transform coordinate system to UTM using EPSG:32610 - WGS 84 / UTM zone 10N
cord.UTM1 <- spTransform(cord.dec1, CRS("+init=epsg:32610"))
cord.UTM2 <- spTransform(cord.dec2, CRS("+init=epsg:32610"))
cord.UTM3 <- spTransform(cord.dec3, CRS("+init=epsg:32610"))
cord.UTM4 <- spTransform(cord.dec4, CRS("+init=epsg:32610"))


# Calculate Euclidean distance (m) from UTM coordinates 
euclidDist1 <- sp::spDists(cord.UTM1, longlat = FALSE, segments=TRUE)
euclidDist2 <- sp::spDists(cord.UTM2, longlat = FALSE, segments=TRUE)
euclidDist3 <- sp::spDists(cord.UTM3, longlat = FALSE, segments=TRUE)
euclidDist4 <- sp::spDists(cord.UTM4, longlat = FALSE, segments=TRUE)

T1_merged$EucDIS <- c(NA, euclidDist1)
T2_merged$EucDIS <- c(NA, euclidDist2)
T3_merged$EucDIS <- c(NA, euclidDist3)
T4_merged$EucDIS <- c(NA, euclidDist4)



# Calculate distance traveled (m) per transect based on Euclidean distance
## input result into transect data csv file
sum(T3$EucDIS, na.rm = TRUE)


# tidy up
remove(cord.dec1, cord.dec2, cord.dec3, cord.dec4, 
       cord.UTM1, cord.UTM2, cord.UTM3, cord.UTM4)

### END distance calculation ----------------------------------------------- ###


## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## calculate seafloor area ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##


## functions ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## function to convert degrees to radians for trigonomic functions
to_rad <- function(deg){
  rad <- deg*(pi/180)
  return(rad)}


## seafloor area calcuation 
seafloor_area <- function(width, height, altitude){
  w1 <- to_rad(width/2)
  h1 <- to_rad(height/2)
  
  area <- (altitude*2*tan(w1)) * (altitude*2*tan(h1))
  width_only <- (altitude*2*tan(w1))
  height_only <- (altitude*2*tan(h1))
  
  df <- data.frame(altitude, width_only, height_only, area)
  colnames(df) <- c("alt", "width", "height", "area")
  return(df)
}


## function to combine seafloor calculation into transect data frame
combine.df <- function(out, df){
  out$width <- df$width
  out$height <- df$height
  out$area <- df$area
  return(out)
}
## END functions ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##



## parameters ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
width_angle <- 109
height_angle <- 63 
low <- 0.1
high <- 2
freq <- 0.1
altitude <- seq(low, high, by=freq)
## END parameters ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##


## invoke functions ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## invoke function to create new dataframe
test_dat <- seafloor_area(width_angle, height_angle, altitude)


## invoke function to calculate a single value
seafloor_area(width_angle, height_angle, 0.75)


## apply to data frame
T1.dat <- seafloor_area(width_angle, height_angle, T1_merged$altitude)
T2.dat <- seafloor_area(width_angle, height_angle, T2_merged$altitude)
T3.dat <- seafloor_area(width_angle, height_angle, T3_merged$altitude)
T4.dat <- seafloor_area(width_angle, height_angle, T4_merged$altitude)


## combine area calculations into transect data frames
T1 <- combine.df(T1_merged, T1.dat)
T2 <- combine.df(T2_merged, T2.dat)
T3 <- combine.df(T3_merged, T3.dat)
T4 <- combine.df(T4_merged, T4.dat)

remove(T1.dat, T2.dat, T3.dat, T4.dat, dat)
## END function invokation ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##



## END seafloor area figures ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##


########## ~~~~~~~~~~~~~~~~~~  Write csv files for each transect  ~~~~~~~~~~~~~~~~~~~  ##########

## save smoothed transects as .csv files
write.csv(T1, "./Dives/2023_09_15/logs/transects/2023_09_15_S5_T1.csv", row.names = FALSE)
write.csv(T2, "./Dives/2023_09_15/logs/transects/2023_09_15_S5_T2.csv", row.names = FALSE)
write.csv(T3, "./Dives/2023_09_15/logs/transects/2023_09_15_S5_T3.csv", row.names = FALSE)
write.csv(T4, "./Dives/2023_09_15/logs/transects/2023_09_15_S5_T4.csv", row.names = FALSE)

## END of transect file creation ---------------------------------------------------- ##



########## ~~~~~~~~~~~~~~~~~~  Create Transect Maps  ~~~~~~~~~~~~~~~~~~~  ##########

map <- leaflet() %>%
  addTiles() %>%
  addPolylines(data=T3, lat=~DVLlat, lng=~DVLlon, weight=1, color="black", opacity=1) %>%
  addPolylines(data=T4, lat=~DVLlat, lng=~DVLlon, weight=1, color="red", opacity=1)

map

## END of map creation ---------------------------------------------------- ##



## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
