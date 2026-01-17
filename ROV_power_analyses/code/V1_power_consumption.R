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
dat <- read.csv(file.path(results, "V1_energy_usage_edited.csv"))
## END startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## minor adjustments ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## create proxy for minutes
#dat$min <- dat$SU/60


## log transform current
#dat$log_current <- log10(dat$current + 1) 


## calculate watts consumed 
#dat$watts <- dat$voltage * dat$current


## set factor
dat$condition <- factor(dat$condition,
                        levels = c("low_current", "high_current"))


## save csv 
#write.csv(dat, file = file.path("results", "V1_energy_usage_edited.csv"), row.names = FALSE)


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
fill_vals <- c(
  low_current  = "#009ACD",  # blue
  high_current = "#B22222"   # red
)

fills <- scale_fill_manual(values = fill_vals)
cols <- scale_color_manual(values = fill_vals)


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


## plot watts consumed  
p5 <- ggplot(dat, aes(x=current, group=condition)) +
  geom_density(aes(fill=condition), alpha=0.65) + my.theme + fills +
  xlab("Electrical current") + ylab("frequency")
print(p5)


## plot watts consumed  
p6 <- ggplot(dat, aes(x=watts, group=condition)) +
  geom_density(aes(fill=condition), alpha=0.65) + my.theme + fills +
  xlab("Watts: Voltage * Current") + ylab("frequency")
print(p6)


## plot low current only 
p7 <- dat %>%
  filter(condition == "low_current") %>%
  ggplot(aes(x = watts, fill = condition)) +
  geom_density(alpha = 0.65) +
  xlim(0, 400) +
  my.theme + fills +
  xlab("Watts: Voltage * Current") + ylab("frequency")
print(p7)
## END plots ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
