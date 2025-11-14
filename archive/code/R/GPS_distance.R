### Calculate distance between GPS points 

#set working directory 
setwd(here::here())


#load packages 
library(geosphere)
library(dplyr)
library(measurements)
library(rgdal)


## bring in cleaned primary telemetry data file
# path to Ping files = ./ROV_telemetry/Ping
# path to telemetry files = ./ROV_telemetry/QGC

dat = read.csv("./ROV_telemetry/QGC/2022-08-15_09-56-00_vehicle1_cleaned.csv")


## Haversine Distance (not using) ------------------------
# create new data frame with Distance column. Distance is the difference in lon, lat 
# between rows converted from degrees to meters using the 'Haversine method'
# dat2 <- dat %>% mutate(dat, HavDIS = distHaversine(cbind(lon, lat), cbind(lag(lon), lag(lat))))


## Euclidean Distance -----------------------

#Convert coordinates to SpatialPoint object 
cord.dec = SpatialPoints(cbind(dat$lon, -dat$lat), proj4string = CRS("+proj=longlat"))

# Transform coordinate system to UTM using EPSG:32610 - WGS 84 / UTM zone 10N
cord.UTM <- spTransform(cord.dec, CRS("+init=epsg:32610"))
head(cord.UTM)

# Calculate Euclidean distance (m) from UTM coordinates 
euclidDist <- sp::spDists(cord.UTM, longlat = FALSE, segments=TRUE)

# Append Euclidean distance values to data frame with first row as NA
dat$EucDIS <- c(NA, euclidDist)

# save csv with Haversine and Euclidean distance columns 
write.csv(dat, './ROV_telemetry/QGC/2022-08-15_09-56-00_vehicle1_cleaned_distance.csv', row.names = FALSE)

## END distance calculation ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
