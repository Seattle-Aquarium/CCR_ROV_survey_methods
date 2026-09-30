## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ROV power consumption graphing
##
## Rebuilds the power-consumption figures in the manuscript:
##   main text Fig. 4   V3 flight: W and Wh over time, log10(W) densities
##   Supp. Fig. S11     V1 flights in low and high current: the same three views
##   Supp. Fig. S12     V3 flight: un-transformed W, stacked histogram
## plus the V4 lighting-gain runs, and the Wh tabulations quoted in the text.
##
## Run from tlog_visualization/code/ (or tlog_visualization/):
##   Rscript visualize_power_consumption.R
## Transect colours (transect_fills in functions.R) are the colour-blind-safe
## Okabe-Ito palette.
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
rm(list = ls())


## read in libraries
library(tidyverse)


## set working directory to tlog_visualization/
if (basename(getwd()) == "code") setwd("../")
getwd()


## relative files paths
data <- "data"
results <- "results"
figs <- "figs"
code <- "code"


## invoke source file to pull in function
source(file.path(code, "functions.R"))


## read in csv
dat_v3 <- read.csv(file.path(results, "V3_power_consumption.csv"))
dat_v1 <- read.csv(file.path(data, "V1_power_consumption.csv"))
dat_v4 <- read.csv(file.path(results, "V4_power_consumption.csv"))

dat_low <- read.csv(file.path(data, "low_water_current.csv"))
dat_high <- read.csv(file.path(data, "high_water_current.csv"))

dat_v3$transect <- as.factor(dat_v3$transect)
dat_v4$transect <- as.factor(dat_v4$transect)
## END startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## V3: main text Fig. 4 and Supp. Fig. S12 ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## (a) watts through time - instantaneous power consumption
v3_W <- power_over_time(dat = dat_v3,
  y_col = W,
  legend_pos = c(0.2, 0.83),
  lw_values = transect_lw_1,
  ylab = "Power consumption (W)"
)


## (b) log10(watts) stacked kernel density
v3_log_W <- density_stack(
  dat = dat_v3,
  x_col = "log_W",
  legend_pos = c(0.85, 0.80),
  xlab = "Power consumption (W)",
  ylab = "Density",
  back_transform = TRUE
)


## (c) Wh through time - cumulative power consumption
v3_Wh <- power_over_time(dat = dat_v3,
  y_col = Wh,
  legend_pos = c(0.2, 0.83),
  lw_values = transect_lw_2,
  ylab = "Power consumption (Wh)"
)


## Fig. S12: un-transformed watts, stacked histogram
v3_W_hist <- power_histogram(dat = dat_v3, x_col = W, bins = 150)
## END V3 ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## V1: Supp. Fig. S11, low vs high water current side by side ~~~~~~~~~~~~~~~~~~
v1_W <- plot_V1(dat = dat_v1,
        y_col = W,
        x_col = min,
        transect_col = transect,
        flight_col = water_current,
        lw_values = transect_lw_1,
        ylab = "Power consumption (W)")


v1_log_W <- V1_density_stack(dat = dat_v1,
                 x_col = log_W,
                 flight_col = water_current,
                 back_transform = TRUE)


v1_Wh <- plot_V1(dat = dat_v1,
        y_col = Wh,
        x_col = min,
        transect_col = transect,
        flight_col = water_current,
        lw_values = transect_lw_1,
        ylab = "Power consumption (Wh)")
## END V1 ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## V4: lighting-gain runs ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
gain_labels <- c(
  "0" = "off transect",
  "1" = "gain = 20%",
  "2" = "gain = 30%",
  "3" = "gain = 30%",
  "4" = "gain = 40%",
  "5" = "gain = 50%"
)

v4_Wh <- power_over_time(dat = dat_v4,
  y_col = Wh,
  labels = gain_labels,
  legend_pos = c(0.15, 0.80),
  lw_values = transect_lw_2,
  ylab = "Power consumption (Wh)"
)

v4_W <- power_over_time(dat = dat_v4,
  y_col = W,
  labels = gain_labels,
  legend_pos = c(0.65, 0.25),
  lw_values = transect_lw_1,
  ylab = "Power consumption (W)"
)

v4_log_W <- density_stack(
  dat = dat_v4,
  x_col = "log_W",
  labels = gain_labels,
  legend_pos = c(0.85, 0.80),
  xlab = "Power consumption (W)",
  ylab = "Density",
  back_transform = TRUE,
  xlim_watts = c(400, 800),
  breaks_watts = seq(400, 800, by = 50)
)
## END V4 ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## save plots (sizes match the manuscript figure files) ~~~~~~~~~~~~~~~~~~~~~~~~
## manuscript file names: V3_W_across_time, V3_log_W_density, V3_Wh_across_time
## (Fig. 4); V1_W_across_time, V1_log_W_density, V1_Wh_across_time (Fig. S11);
## natural_scale_V3_density (Fig. S12)
save_fig(v3_W,      filename = "W_across_time",            subfolder = "V3", width = 713 / 72, height = 353 / 72, dpi = 600)
save_fig(v3_log_W,  filename = "log_W_density",            subfolder = "V3", width = 10,       height = 5,        dpi = 600)
save_fig(v3_Wh,     filename = "Wh_across_time",           subfolder = "V3", width = 10,       height = 5,        dpi = 600)
save_fig(v3_W_hist, filename = "natural_scale_V3_density", subfolder = "V3", width = 653 / 72, height = 353 / 72, dpi = 600)

save_fig(v1_W,      filename = "V1_W_through_time",        subfolder = "V1", width = 896 / 72, height = 353 / 72, dpi = 600)
save_fig(v1_log_W,  filename = "V1_log_watts_density",     subfolder = "V1", width = 896 / 72, height = 353 / 72, dpi = 600)
save_fig(v1_Wh,     filename = "V1_Wh_through_time",       subfolder = "V1", width = 896 / 72, height = 353 / 72, dpi = 600)

save_fig(v4_Wh,     filename = "V4_Wh_across_time",        subfolder = "V4", width = 10,       height = 5,        dpi = 600)
save_fig(v4_W,      filename = "V4_W_across_time",         subfolder = "V4", width = 10,       height = 5,        dpi = 600)
save_fig(v4_log_W,  filename = "V4_log_W_density",         subfolder = "V4", width = 10,       height = 5,        dpi = 600)
## END plot save ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## Wh consumed on and off transect, and per minute (results/*.txt) ~~~~~~~~~~~~~
## V3 flight (Fig. 4)
tabulate_wh(dat_v3,
            time_col = "Time",
            out_file = "V3_Wh_power_consumption.txt")


## V1 flights (Fig. S11): high and low water current
V1_tabulate_wh(dat_high,
               wh_col = "Wh",
               time_col = "min",
               time_format = "minutes",
               transect_col = "transect",
               out_file = "Wh_consumption.txt")

V1_tabulate_wh(dat_low,
               wh_col = "Wh",
               time_col = "min",
               time_format = "minutes",
               transect_col = "transect",
               out_file = "V1_Wh_consumption_low_current.txt")
## END Wh calculations ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
