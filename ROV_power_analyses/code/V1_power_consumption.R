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
dat <- read.csv(file.path(results, "V1_energy_usage.csv"))
## END startup ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## minor adjustments ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## filter to low-current 
dat <- dat %>% filter(condition == "low_current")


## function to add log10 Watts at the 6th position 
log_W <- function(dat, source_col = "W") {
  
  dat %>%
    dplyr::mutate(log_W = log10(.data[[source_col]] + 1)) %>%
    dplyr::relocate(log_W, .after = dplyr::everything()[5])
}


## invoke function 
#dat <- log_W(dat)


## function to add transect number to the dataframe
add_transect_column <- function(dat,
                                windows,
                                time_col = "Timestamp",
                                col_name = "transect") {

  parse_mmss <- function(x) {
    parts <- strsplit(as.character(x), ":", fixed = TRUE)
    sapply(parts, function(p) {
      as.numeric(p[1]) * 60 + as.numeric(p[2])
    })
  }
  
  # parse times
  t_vals <- parse_mmss(dat[[time_col]])
  
  # initialize as 0 (not on transect)
  tr <- rep(0L, nrow(dat))
  
  # assign transect IDs in the order provided
  for (i in seq_along(windows)) {
    start_i <- parse_mmss(windows[[i]][1])
    end_i   <- parse_mmss(windows[[i]][2])
    
    tr[t_vals >= start_i & t_vals <= end_i] <- i
  }
  
  # write factor column (levels 0..max)
  dat[[col_name]] <- factor(tr, levels = 0:max(tr))
  
  # move the new column to the 2nd position
  idx_new <- which(names(dat) == col_name)
  dat <- dat[, c(1, idx_new, setdiff(seq_along(dat), c(1, idx_new)))]
  
  dat
}


## invoke function to add transect times
#dat <- add_transect_column(
#  dat,
#  windows = list(
#    c("02:34.7", "15:06.0"),
#    c("17:01.1", "29:02.4")
#  )
#)


## save csv 
#write.csv(dat, file = file.path("results", "V1_energy_usage.csv"), row.names = FALSE)
## END data prep ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## set up custom ggplot theme ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
dat$transect <- as.factor(dat$transect)


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


## transect colors
transect_fills <- c(
  "0" = "gray",
  "1" = "#308014",
  "2" = "#104E8B",
  "3" = "#B22222"
)


## linewidth mapping (edit as you like)
transect_lw_1 <- c(
  "0" = 0.35,
  "1" = 0.70,
  "2" = 0.70,
  "3" = 0.70
)


transect_lw_2 <- c(
  "0" = 1,
  "1" = 1.5,
  "2" = 1.5,
  "3" = 1.5
)


## open graphing windows               
graphics.off()
windows(10,5,record = T)
## END transformations and graphing set up ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## create plots ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## plot of watt power consumption through time or Ah consumption through time
p1 <- ggplot(dat, aes(x = min, y = Ah, 
    group = 1,                 # single continuous path (time series)
    color = transect,
    linewidth = transect)) +
  geom_path(alpha = 0.95) +
  scale_color_manual(
    values = transect_fills,
    labels = c(
      "0" = "off transect",
      "1" = "transect 1",
      "2" = "transect 2",
      "3" = "transect 3"
    )
  ) +
  scale_linewidth_manual(
  values = transect_lw_2,
  guide = "none"             # keep legend format clean (like your p7)
  ) +
  
  my.theme +
  xlab("ROV flight time") + ylab("Ah consumed") +
  theme(
    legend.position   = c(0.15, 0.80),
    legend.title      = element_blank(),
    legend.background = element_rect(fill = "white", colour = "black"),
    legend.key        = element_rect(fill = NA)
  )

print(p1)



## plot of log10(x+1) Watt power consumption 
p2 <- ggplot(dat, aes(x = log_W, fill = transect)) +
  geom_density(position = "stack", alpha = 0.85) +
  
  scale_fill_manual(
    values = transect_fills,
    labels = c(
      "0" = "off transect",
      "1" = "transect 1",
      "2" = "transect 2"
    )
  ) +
  
  scale_x_continuous(
    labels = function(x) round(10^x),
    expand = expansion(mult = c(0.02, 0.08))  # <-- key line
  ) +
  
  my.theme +
  xlab("Watts consumed") + ylab("Density") +
  theme(
    axis.text.y   = element_blank(),
    axis.ticks.y  = element_blank(),
    legend.position = c(0.85, 0.80),
    legend.title    = element_blank(),
    legend.background = element_rect(fill = "white", colour = "black"),
    legend.key      = element_rect(fill = NA)
  )

print(p2)

## END plots ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
