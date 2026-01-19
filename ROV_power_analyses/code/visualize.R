## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## Species richness curves for CCR analysis of Urban Kelp data  
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


## invoke source file to pull in function 
source(file.path(code, "functions.R"))


## read in csv 
dat <- read.csv(file.path(results, "V3_power_consumption.csv"))
#write.csv(dat, file = file.path("results", "V1_energy_usage.csv"), row.names = FALSE)
## END startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## set up custom ggplot theme ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
dat$transect <- as.factor(dat$transect)


## open graphing windows               
graphics.off()
windows(10,5,record = T)
## END set up `~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## create plots ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## plot of Ah consumed through time 
p1 <- power_over_time(dat = dat,
  y_col = Wh,
  legend_pos = c(0.15, 0.80),
  lw_values = transect_lw_2,
  ylab = "Power consumption (Wh)"
)

print(p1)


## plot of watts conusmed through time
p2 <- power_over_time(dat = dat,
  y_col = W,
  legend_pos = c(0.60, 0.80),
  lw_values = transect_lw_1,
  ylab = "Power consumption (W)"
)

print(p2)


## invoke function to plot kernel density stack
p3 <- density_stack(
  dat = dat,
  x_col = log_W,
  legend_pos = c(0.85, 0.80),
  xlab = "Power consumption (W)",
  ylab = "Density",
  adjust = 1
)

print(p3)
## END plots ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
