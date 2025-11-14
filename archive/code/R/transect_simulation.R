## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## simulate transects ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## initiate ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
rm(list = ls())
library(tidyverse)
library(leaflet)

T1 <- read.csv("2022_10_06_T1.csv")
T2 <- read.csv("2022_10_06_T2.csv")
T3 <- read.csv("2022_10_06_T3.csv")
T4 <- read.csv("2022_10_06_T4.csv")
steps <- read.csv("steplengths.csv")
## END initiate ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## function to select transect data and convert to radians ~~~~~~~~~~~~~~~~~~ ##
load.dat <- function(transect.data){
  dat <- transect.data ## select which transect to work upon 
  pi <- 3.141593 
  two_pi <- pi*2
  angle <- two_pi/360 ## calculation to enable conversion to radians
  dat$heading_degrees <- dat$heading ## create new column 
  dat$radians <- dat$heading_degrees*angle ## convert decimal degrees to radians
  return(dat)
}
## END function to select / convert data ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## function to simulate transects ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
movement <- function(xy, step, heading) {
  x_init <- xy[1,1] ## starting point for x 
  y_init <- xy[1,2] ## starting point for y 
  
  y_change <- sin(heading)*step ## using heading and sine function, specify change in y
  y_new <- y_init + y_change
  x_change <- cos(heading)*step ## using heading and cosine, specify change in x 
  x_new <- x_init + x_change
  
  move.temp <- as.data.frame(matrix(0,1,4)) ## create a data frame to save coordinates
  move.temp[1,1] <- x_new
  move.temp[1,2] <- y_new
  move.temp[1,3] <- step
  move.temp[1,4] <- heading
  
  return(move.temp)
}
## END function to simulate transects ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## function to create dataframe output for simulated steps and coordinates ~~ ##
set.output <- function(transect.data, starting.x.coord, starting.y.coord){
  steps.df <- data.frame(matrix(0, nrow(transect.data),4))
  steps.df[1,1] <- starting.x.coord
  steps.df[1,2] <- starting.y.coord
  colnames(steps.df) <- c("x", "y", "step.length", "turn.angle")
  return(steps.df)
}
## END function to create output df ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## for loop to simulate transects ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
simulate <- function(transect.data, output.df){
  for (i in 2:nrow(transect.data)) {
    step <- sample(steps$smooth_dist, 1, replace=TRUE)
    heading <- transect.data$radians[i]
    next.pt <- movement(output.df[(i-1),1:2], step, heading)
    output.df[i,] <- next.pt
  }
  return(output.df)
}
## END for loop to simulate transects ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## append new data to transect dataframe and save ~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
save.data <- function(transect.data, new.data, file.name){
  transect.data$sim_x <- new.data$x
  transect.data$sim_y <- new.data$y
  transect.data$sim_steps <- new.data$step.length
  write.csv(transect.data, file.name)
  return(transect.data)
}
## END data save ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## invoke functions to simulate transects ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## load data and convert to radians
dat_T1 <- load.dat(T1)
dat_T2 <- load.dat(T2)
dat_T3 <- load.dat(T3)
dat_T4 <- load.dat(T4)


## create dataframe with output; specify starting x, y coordinates 
out_T1 <- set.output(dat_T1, dat_T1$lat[1], dat_T1$lon[1]) #47.63037
out_T2 <- set.output(dat_T2, dat_T2$lat[1], dat_T2$lon[1])
out_T3 <- set.output(dat_T3, dat_T3$lat[1], dat_T3$lon[1])
out_T4 <- set.output(dat_T4, dat_T4$lat[1], dat_T4$lon[1])


## simulate data 
out_T1 <- simulate(dat_T1, out_T1)
out_T2 <- simulate(dat_T2, out_T2)
out_T3 <- simulate(dat_T3, out_T3)
out_T4 <- simulate(dat_T4, out_T4)


## save data 
dat_T1 <- save.data(dat_T1, out_T1, "simulated_2022_10_06_T1.csv")
dat_T2 <- save.data(dat_T2, out_T2, "simulated_2022_10_06_T2.csv")
dat_T3 <- save.data(dat_T3, out_T3, "simulated_2022_10_06_T3.csv")
dat_T4 <- save.data(dat_T4, out_T4, "simulated_2022_10_06_T4.csv")
## END data simulation ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## plot simulated data ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
map <- leaflet() %>%
  addTiles() %>%
  addPolylines(data=T1, lat=~lat_smooth, lng=~lon_smooth, weight=1, color="red", opacity=1)%>%
  addPolylines(data=T3, lat=~lat_smooth, lng=~lon_smooth, weight=1, color="red", opacity=1)%>%
  addPolylines(data=dat_T2, lat=~sim_x, lng=~sim_y, weight=1, color="black", opacity=1)%>%
  addPolylines(data=dat_T4, lat=~sim_x, lng=~sim_y, weight=1, color="black", opacity=1)

map
## END plot ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
