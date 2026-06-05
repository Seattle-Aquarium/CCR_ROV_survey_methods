## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## code to wrange ROV telemetry data   
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
source(file.path(code, "functions.R"))


## read in csv 
dat <- read.csv(file.path(data, "Lutris_V4.csv"))
## END startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## invoke functions to wrangle data ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## combine multiple tlogs
dat <- append_tlogs(dat, dat2)


## add Ah column 
dat <- add_battery_ah(dat)


## add SU and min columns 
dat <- add_su_and_min(dat)


## rename columns for easy referencing 
dat <- rename_battery_columns(dat)


## conduct log10(x+1) transform on Watts column  
dat <- log_W(dat)


## remove rows while ROV on deck - minimum Watts = 50 power draw 
dat <- preflight_trim(dat, threshold = 0)


## add column for transects based on real-world survey flight times
dat <- add_transect_column(dat, windows = list(
    c("09:21:52", "09:23:40"),
    c("09:24:50", "09:26:52"),
    c("09:27:31", "09:31:31"),
    c("09:34:16", "09:39:16"),
    c("09:40:12", "09:45:12")
  )
)
## END data wrangling ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## save and close ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
write.csv(dat, file = file.path("results", "V4_power_consumption.csv"), row.names = FALSE)
## END csv file creation ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
