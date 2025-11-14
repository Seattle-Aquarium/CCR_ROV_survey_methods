## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## GPS tracklog cleanup and visualization -- Seattle Aquarium ROV dev ~~~~~~~ ##
## August 4 2022 -- zhr ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
library(tidyverse)
library(sf)
library(rnaturalearth)
library(rnaturalearthdata)
library(leaflet)
rm(list = ls())

## set your path where you saved the GPS tracklog 
path <- "D:/OneDrive/Desktop"

getwd()
## load a path
setwd(path)
dat2 <- read.csv("test.csv", header = TRUE, stringsAsFactors = FALSE)
## END startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## process columns ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## filter columns
filtered <- dat[c("heading", "altitudeRelative", "flightTime", 
                "battery0.percentRemaining", "clock.currentTime",
                "clock.currentDate","gps.lat","gps.lon","gps.count",
                "temperature.temperature2")]


## rename coluymns
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


## reorder columns
ordered <- renamed[, c(6, 5, 3, 2, 1, 4, 7, 8, 9, 10)]


## create sequence of minutes to plot along x-axis
ordered$seq <- seq(1, nrow(ordered), by=1) / 60


## function to create a running dive clock column 
x.max <- function(df){
  s1 <- nrow(df)/60
  s2 <- round(s1, digits=0)
  return(s2)
}


## call function
xlim <- x.max(dat)


## remove erroneous GPS points 
filtered <- subset(ordered, lon < -124.7105)


## tidy up
remove(renamed, ordered)


## save csv file
write.csv(filtered, "2022-08-01_13-23_cleaned.csv")
## END column processing ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##





## plot dive profile ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
windows(10,5,record = T)


## set up custom ggplot theme 
my.theme = theme(panel.grid.major = element_blank(), 
                 panel.grid.minor = element_blank(),
                 panel.background = element_blank(), 
                 axis.line = element_line(colour = "black"),
                 axis.title.x=element_text(size=15),
                 axis.title.y=element_text(size=15),
                 axis.text=element_text(size=15),
                 plot.title = element_text(size=15))



## plot depth
p1 <- ggplot(dat, aes(seq, depth)) +
  geom_path() + my.theme + 
  scale_x_continuous(breaks=seq(0, xlim, 4)) +
  xlab("dive time (minutes)") + ylab("depth (meters)") +
  geom_hline(yintercept=0, col="blue")

print(p1)
## END depth figure ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##

graphics.off()
windows(10, 5)


## create interactive leaflet map ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
map <- leaflet() %>%
  addTiles() %>%
  #setView(lng = -124.7107, lat = 48.38889, zoom=15) %>%
  addPolylines(data=dat2, lat=~gps.lat, lng=~gps.lon, weight=1, color="black", opacity=1)


## visualize map 
map
## END map creation ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##



graphics.off()

## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##
