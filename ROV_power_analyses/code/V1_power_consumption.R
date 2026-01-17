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


## read in csv 
dat <- read.csv(file.path(data, "V1_energy_usage.csv"))
## END startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## minor adjustments ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## create proxy for minutes
dat$min <- dat$SU/60


## log transform current
dat$log_current <- log10(dat$current + 1) 


## set up custom ggplot theme 
my.theme = theme(panel.grid.major = element_blank(), 
                 panel.grid.minor = element_blank(),
                 panel.background = element_blank(), 
                 axis.line = element_line(colour = "black"),
                 axis.title.x=element_text(size=15),
                 axis.title.y=element_text(size=15),
                 axis.text=element_text(size=15),
                 plot.title = element_text(size=15),
                 legend.title=element_text(size=13), 
                 legend.text=element_text(size=13))


## set colors 
cols <- scale_color_manual(values=c("#B22222", "#009ACD"))
fills <- scale_fill_manual(values=c("#B22222", "#009ACD"))


## open graphing windows               
windows(10,5,record = T)
## END transformations and graphing set up ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## create plots ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## plot mAh consumed 
p1 <- ggplot(dat, aes(min, mAh, group=condition)) +
  geom_path(aes(col=condition), lwd=1.2) + my.theme + cols + 
  xlab("flight time (min)") + ylab("mAh consumed") 
print(p1)


## plot voltage consumed 
p2 <- ggplot(dat, aes(min, voltage, group=condition)) +
  geom_path(aes(col=condition)) + my.theme + cols +
  xlab("flight time (min)") + ylab("voltage") 
 print(p2)
 
 
## plot log10 current consumed  
p3 <- ggplot(dat, aes(x=log_current, group=condition)) +
  geom_density(aes(fill=condition), alpha=0.65) + my.theme + fills +
  xlab("log10(current + 1)") + ylab("frequency")
print(p3)


## plot current consumed 
p4 <- ggplot(dat, aes(x=current, group=condition)) +
  geom_density(aes(fill=condition), alpha=0.65) + my.theme + fills +
  xlab("Electrical current") + ylab("frequency")
print(p4)
## END plots ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
