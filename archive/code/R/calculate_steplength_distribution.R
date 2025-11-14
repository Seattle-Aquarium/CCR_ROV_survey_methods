## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## calculate distribution of steplengths ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## initiate ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
rm(list = ls())
library(tidyverse)
steplengths <- read.csv("original_steplengths.csv")
## END initiate ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## calculate the distance traveled between steplengths ~~~~~~~~~~~~~~~~~~~~~~ ##
n <- nrow(steplengths)
smooth_steps <- steplengths
smooth_steps$smooth_dist<-NA
smooth_steps$smooth_dist[2:n] <- sqrt((steplengths$lat_smooth[2:n] - steplengths$lat_smooth[1:n-1]) ^ 2 + 
                                        (steplengths$lon_smooth[2:n] - steplengths$lon_smooth[1:n-1]) ^ 2)

## remove 1st row with NA value
smooth_steps <- smooth_steps[-1, ]
## END steplength calculation ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##




## visualize data ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## upscale to visually interpret
smooth_steps$smooth_dist <- smooth_steps$smooth_dist * 100000

## plot histogram
graphics.off()
windows(8, 5, record=T)

## theme for plotting
my.theme = theme(panel.grid.major = element_blank(),
                 panel.grid.minor = element_blank(),
                 panel.background = element_blank(), 
                 axis.line = element_line(colour = "black"),
                 axis.text  = element_text(size=14), 
                 axis.title = element_text(size=16))

## plot 
ggplot(data=smooth_steps, aes(smooth_dist)) + 
  geom_histogram(binwidth=.015) + xlim(-0.1, 1) + my.theme +
  xlab("smoothed & rescaled steplengths") +
  ylab("frequency")


## remove rows containing anomolously large movements
smooth_steps <- subset(smooth_steps, smooth_dist < 0.50)

## convert back to decimal degrees
smooth_steps$smooth_dist <- smooth_steps$smooth_dist / 100000

## save file 
write.csv(smooth_steps, "steplengths.csv")
## END steplength calculation / filtering ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
