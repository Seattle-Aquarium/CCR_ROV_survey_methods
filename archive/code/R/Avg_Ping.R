#set working directory 
setwd(here::here())

## load packages
library(tidyverse)
library(lubridate)  ## lubridate should be in tidyverse, but adding it here just in case
library(hms)
library(measurements)


## load Ping data
# path to Ping files = ./ROV_telemetry/Ping
dat <- read.csv("./ROV_telemetry/Ping/20220815-115551033.csv", header=TRUE)

## rename columns 
names(dat)[2] <- "dist"
names(dat)[3] <- "conf"


## Convert altimeter data from mm to m
dat$dist <- conv_unit(dat$dist, "mm", "m")


## function to calculate average ping and confidence per 1s 
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


## call function
new_dat <- Avg.Ping(dat)

write.csv(new_dat, './ROV_telemetry/Ping/20220815-115551033_Avg_Ping.csv', row.names = FALSE)
  
  
  

