## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## calculate seafloor area ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## initiate ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
rm(list = ls())
library(tidyverse)
T1 <- read.csv("simulated_2022_10_06_T1.csv")
T2 <- read.csv("simulated_2022_10_06_T2.csv")
T3 <- read.csv("simulated_2022_10_06_T3.csv")
T4 <- read.csv("simulated_2022_10_06_T4.csv")
## END initiate ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## functions ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## function to convert degrees to radians for trigonomic functions
to_rad <- function(deg){
  rad <- deg*(pi/180)
  return(rad)}


## seafloor area calcuation 
seafloor_area <- function(width, height, ping){
  w1 <- to_rad(width/2)
  h1 <- to_rad(height/2)
  
  area <- (ping*2*tan(w1)) * (ping*2*tan(h1))
  width_only <- (ping*2*tan(w1))
  height_only <- (ping*2*tan(h1))
  
  df <- data.frame(ping, width_only, height_only, area)
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
width_angle <- 87#109
height_angle <- 56#63 
low <- 0.1
high <- 2
freq <- 0.1
ping_values <- seq(low, high, by=freq)
## END parameters ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## invoke functions ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## invoke function to create new dataframe
test_dat <- seafloor_area(width_angle, height_angle, ping_values)


## invoke function to calculate a single value
seafloor_area(width_angle, height_angle, 1.05)



## apply to data frame
T1.dat <- seafloor_area(width_angle, height_angle, T1$avg_dist)
T2.dat <- seafloor_area(width_angle, height_angle, T2$avg_dist)
T3.dat <- seafloor_area(width_angle, height_angle, T3$avg_dist)
T4.dat <- seafloor_area(width_angle, height_angle, T4$avg_dist)


## combine area calculations into transect data frames
T1 <- combine.df(T1, T1.dat)
T2 <- combine.df(T2, T2.dat)
T3 <- combine.df(T3, T3.dat)
T4 <- combine.df(T4, T4.dat)
remove(T1.dat, T2.dat, T3.dat, T4.dat, dat)
## END function invokation ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## seafloor area figures ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ## 
## plot data 
graphics.off()
windows(8,5,record=T)

## theme for plotting
my.theme = theme(panel.grid.major = element_blank(),
                 panel.grid.minor = element_blank(),
                 panel.background = element_blank(), 
                 axis.line = element_line(colour = "black"),
                 axis.text  = element_text(size=14), 
                 axis.title = element_text(size=16))


## print relationship between Ping & seafloor area  
p1 <- ggplot(data=dat, aes(alt, area)) + 
  geom_vline(xintercept=0.75, color="gray", lwd=0.25) + 
  geom_vline(xintercept=1.3, color="gray", lwd=0.25) +
  geom_hline(yintercept=1.933, color="gray", lwd=0.25) + 
  geom_hline(yintercept=5.807, color="gray", lwd=0.25) +
  geom_path(aes(alt, width), color="#00688B") +
  geom_path(aes(alt, height), color="#8C1717") +
  geom_path(lwd=1) + my.theme + 
  xlab("ROV altitude above seafloor (m)") +
  ylab(expression(paste("Seafloor filmed (m or m^2)"))) +
  scale_y_continuous(limits = c(0, 14), breaks = seq(0,14,by=2)) +
  scale_x_continuous(limits = c(0, 2), breaks = seq(0,2,by=0.2)) 
print(p1)


## print transect time vs area surveyed
p2 <- ggplot(data=T1, aes(clock_min, width)) + geom_path(lwd=1) + my.theme +
  xlab("Transect time (min)") + ylab("Seafloor area surveyed (m^2)") +
  scale_x_continuous(limits = c(0, 8), breaks = seq(0,8,by=1)) +
  scale_y_continuous(limits = c(1, 6), breaks = seq(1,6,by=1)) 
print(p2)


## code to make legend for Ping & seafloor area
legend.dat <- pivot_longer(dat, c(width, height, area))
names(legend.dat)[2] <- "response"
my.cols <- c("black", "#8C1717", "#00688B")

p3 <- ggplot(legend.dat, aes(alt, value, group = response, color=response)) + geom_path(lwd=1) +
  scale_color_manual("Camera field of view", values = my.cols, labels=c("Total area (m^2)","Height (m)","Width (m)")) +
  theme(legend.title = element_text(size=14), 
        legend.text = element_text(size=14),
        legend.key.size = unit(1, 'cm')) 
print(p3)
## END seafloor area figures ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
