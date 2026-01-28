## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## surftrak graphing
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
rm(list = ls())


## read in libraries 
library(tidyverse)


## set working directory
setwd("../")
getwd()


## relative files paths 
data <- "data"
results <- "results"
figs <- "figs"
code <- "code"


## read in functions from source file 
#source(file.path(code, "functions.R"))


## read in csv 
#dat <- read.csv(file.path(data, "2025_10_08_T1.csv"))
#dat2 <- read.csv(file.path(data, "2025_10_08_T2.csv"))
dat <- read.csv(file.path(results, "V3_power_consumption.csv"))
## END startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## wrangle data ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## set surftrak to 0,8m
dat$range_finder <- 0.8


## calculate seafloor: (-ROV depth) + (-ROV altitude)
dat$seafloor <- dat$Depth + (-dat$Altitude) 


## calculate ideal altitude for surftrak: (-seafloor) + range_finder
dat$range_set <- dat$seafloor + dat$range_finder


## isolate transect desired
t3 <- dat %>% filter(transect == 3)
## END data wrangling ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## graphing parameters
my.theme = theme(panel.grid.major = element_blank(), 
                 panel.grid.minor = element_blank(),
                 panel.background = element_blank(), 
                 axis.line = element_line(colour = "black"),
                 axis.title.x=element_text(size=15),
                 axis.title.y=element_text(size=15),
                 axis.text=element_text(size=15),
                 plot.title = element_text(size=15),
                 legend.title=element_text(size=15), 
                 legend.text=element_text(size=15))


## open separate visualization 
graphics.off()
windows(10, 5, record=T)
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## quick plot to check surftrak data
p1 <- quick_plot(t3)
print(p1)


## full figure with #2 insets of suftrak performance 
plot_surftrak_insets(
  t3,
  zoom1 = c(52.5, 53.5),
  zoom2 = c(60, 61),
  inset_lwd = 0.45,
  pad_m = 1,
  legend_pos = c(0.13, 0.75)  # upper-left inside panel
)


## plot the ROV's altitude to evaluate surftrak performance
surftrak_density(t3)
## end plots ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
