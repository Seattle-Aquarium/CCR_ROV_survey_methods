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
dat <- read.csv(file.path(results, "V3_power_consumption.csv"))
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
dat <- preflight_trim(dat, threshold = 50)


## add column for transects based on real-world survey flight times
dat <- add_transect_column(dat, windows = list(
    c("10:22:41", "10:31:06"),
    c("11:08:03", "11:16:12"),
    c("11:23:39", "11:34:03")
  )
)
## END data wrangling ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## save and close ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
write.csv(dat, file = file.path("results", "V3_power_consumption.csv"), row.names = FALSE)
## END csv file creation ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
