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
<<<<<<< Updated upstream
dat <- read.csv(file.path(results, "V4_power_consumption.csv"))
=======
dat <- read.csv(file.path(results, "V3_power_consumption.csv"))
dat <- read.csv(file.path(data, "V1_power_consumption.csv"))
dat <- read.csv(file.path(data, "2026_04_02_LightTest_T1.csv"))



dat_low <- read.csv(file.path(data, "low_water_current.csv"))
dat_high <- read.csv(file.path(data, "high_water_current.csv"))

>>>>>>> Stashed changes
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
  legend_pos = c(0.65, 0.25),
  lw_values = transect_lw_1,
  ylab = "Power consumption (W)"
)

print(p2)


## invoke function to plot kernel density stack
p3 <- density_stack(
  dat = dat,
  x_col = "log_W",
  transect_col = "transect",
  fills = transect_fills,
  labels = c(
    "0" = "off transect",
    "1" = "gain = 20%",
    "2" = "gain = 30%",
    "3" = "gain = 30%",
    "4" = "gain = 40%",
    "5" = "gain = 50%"
  ),
  alpha = 0.85,
  legend_pos = c(0.85, 0.80),
  xlab = "Power consumption (W)",
  ylab = "Density",
  adjust = 1,
  bw = NULL,
  back_transform = TRUE,
  expand_mult = c(0.02, 0.15),
  xlim_watts = c(400, 800),
  breaks_watts = seq(400, 800, by = 50)
)

print(p3)
## end of V3 figs ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## plot V1 functions ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
plot_V1(dat = dat,
        y_col = Wh,
        x_col = min,
        transect_col = transect,
        flight_col = water_current,
        lw_values = transect_lw_1,
        ylab = "Power consumption (Wh)")


## plot V1 side-by-side density stack
V1_density_stack(dat = dat,
                 x_col = log_W,
                 flight_col = water_current,
                 back_transform = TRUE)
## END of V1 visualizations ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## save plots ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## Wh through time - cumulative power consumption
save_fig(
  p1, 
  filename = "Wh_across_time", 
  subfolder = "V4", 
  width = 10, 
  height = 5, 
  dpi = 600)


## Watts across time - instantaneous power consumption 
save_fig(
  p2, 
<<<<<<< Updated upstream
  filename = "W_across_time", 
  subfolder = "V4", 
=======
  filename = "W_across_time_test", 
  subfolder = "V3", 
>>>>>>> Stashed changes
  width = 10, 
  height = 5, 
  dpi = 600)


## log10(watts) stacked kernel density
save_fig(
  p3, 
  filename = "log_W_density", 
  subfolder = "V4", 
  width = 10, 
  height = 5, 
  dpi = 600)
## END plot save ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## calculate V3 total Wh consumed, Wh per 10min, Wh per min ~~~~~~~~~~~~~~~~~~~~
tabulate_wh(dat_low)


## tabulate V1 metrics of power consumption
V1_tabulate_wh(dat_high,
               wh_col = "Wh",
               time_col = "min",
               time_format = "minutes",
               transect_col = "transect")
## END Wh calculations ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

