#set working directory 
setwd(here::here())

#load libraries
library(tidyverse)
library(ggplot2)


## Set paths and read in main telemetry file and Ping file from same transect 
# path to Ping files = ./ROV_telemetry/Ping
# path to telemetry files = ./ROV_telemetry/QGC

main = read.csv("./ROV_telemetry/QGC/2022-08-15_09-56-00_vehicle1_cleaned_distance.csv")
Ping = read.csv("./ROV_telemetry/Ping/20220815-095728695_Avg_Ping.csv")

#drop unnecessary X column
main <- subset(main, select = -X)

#Merge data frames together based on time 
main_ping <- merge(main, Ping, by.x = "time")


