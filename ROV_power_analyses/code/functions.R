## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## Functions to process and analyze ROV power consumption
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~




## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## funcion to add Ah column to data 
add_battery_ah <- function(dat) {
  Battery_Ah_used <- dat$Battery_mAh_used / 1000
  dat <- cbind(
    dat[, 1:7],
    Battery_Ah_used = Battery_Ah_used,
    dat[, 8:ncol(dat)]
  )
  dat
}

## add Ah 
#dat <- add_battery_ah(dat)
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## function to streamline column names
rename_battery_columns <- function(dat) {
  dat %>%
    dplyr::rename(
      W   = Battery_W,
      A   = Battery_A,
      V   = Battery_V,
      Wh  = Battery_Wh_used,
      mAh = Battery_mAh_used,
      Ah  = Battery_Ah_used
    )
}

## rename columns
#dat <- rename_battery_columns(dat)
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~




## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## add SU and min cols to beginning to data frame 
add_su_and_min <- function(dat) {
  SU  <- seq_len(nrow(dat))
  min <- round(SU / 60, 2)
  dat <- cbind(
    SU  = SU,
    min = min,
    dat
  )
  dat
}

## add SU and min columns 
#dat <- add_su_and_min(dat)
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## function to add log10 Watts at the 6th position 
log_W <- function(dat, source_col = "W") {
  
  dat %>%
    dplyr::mutate(log_W = log10(.data[[source_col]] + 1)) %>%
    dplyr::relocate(log_W, .after = dplyr::everything()[9])
}

## invoke function 
#dat <- log_W(dat)
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## delete rows at the beginning when the ROV is powered on deck 
preflight_trim <- function(dat, threshold = 50) {
  idx <- which(dat$W >= threshold)[1]
  if (is.na(idx)) {
    return(dat)  
  }
  dat[idx:nrow(dat), ]
}

## remove rows while ROV on deck 
#dat <- preflight_trim(dat, threshold = 50)
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## function to add transect numbers as a column to the data frame
## function to add transect numbers as a column to the data frame
add_transect_column <- function(dat,
                                windows,
                                time_col = "Time",
                                col_name = "transect") {
  
  # Parse time strings of the form:
  # "MM:SS", "MM:SS.ss", "HH:MM:SS", "HH:MM:SS.ss"
  parse_time_to_sec <- function(x) {
    parts <- strsplit(as.character(x), ":", fixed = TRUE)
    
    sapply(parts, function(p) {
      nums <- as.numeric(p)
      
      if (length(nums) == 2) {
        # MM:SS
        mins <- nums[1]
        secs <- nums[2]
        mins * 60 + secs
        
      } else if (length(nums) == 3) {
        # HH:MM:SS
        hrs  <- nums[1]
        mins <- nums[2]
        secs <- nums[3]
        hrs * 3600 + mins * 60 + secs
        
      } else {
        stop("Time format must be MM:SS or HH:MM:SS (optionally with decimals).")
      }
    })
  }
  
  # parse timestamps in the dataframe
  t_vals <- parse_time_to_sec(dat[[time_col]])
  
  # initialize as 0 (not on transect)
  tr <- rep(0L, nrow(dat))
  
  # assign transect IDs in the order provided
  for (i in seq_along(windows)) {
    start_i <- parse_time_to_sec(windows[[i]][1])
    end_i   <- parse_time_to_sec(windows[[i]][2])
    
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
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





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


## transect colors
transect_fills <- c(
  "0" = "gray",
  "1" = "#308014",
  "2" = "#104E8B",
  "3" = "#B22222"
)


## linewidth mapping - narrow for Watts
transect_lw_1 <- c(
  "0" = 0.35,
  "1" = 0.70,
  "2" = 0.70,
  "3" = 0.70
)


## linewidth mapping - thicker, for Ah consumed
transect_lw_2 <- c(
  "0" = 1,
  "1" = 1.5,
  "2" = 1.5,
  "3" = 1.5
)
## END graphing parameters ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~-





## plot watts or Ah consumed over time ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
power_over_time <- function(dat,
                            y_col,
                            x_col = "min",
                            transect_col = "transect",
                            fills = transect_fills,
                            lw_values = transect_lw_2,
                            legend_pos = c(0.15, 0.80),
                            xlab = "ROV flight time",
                            ylab = NULL,
                            alpha = 0.95,
                            order_by_x = TRUE) {
  # y_col can be "Ah" (string) or unquoted Ah; same for x_col, transect_col
  y_col <- rlang::as_name(rlang::ensym(y_col))
  x_col <- rlang::as_name(rlang::ensym(x_col))
  transect_col <- rlang::as_name(rlang::ensym(transect_col))
  
  if (order_by_x && x_col %in% names(dat)) {
    dat <- dat[order(dat[[x_col]]), ]
  }
  
  # Default y-axis label to the column name if not provided
  if (is.null(ylab)) ylab <- y_col
  
  ggplot(
    dat,
    aes(
      x = .data[[x_col]],
      y = .data[[y_col]],
      group = 1,
      color = .data[[transect_col]],
      linewidth = .data[[transect_col]]
    )
  ) +
    geom_path(alpha = alpha) +
    scale_color_manual(
      values = fills,
      labels = c(
        "0" = "off transect",
        "1" = "transect 1",
        "2" = "transect 2",
        "3" = "transect 3"
      )
    ) +
    guides(
      color = guide_legend(
        override.aes = list(linewidth = 1.5)
      )
    ) +
    scale_linewidth_manual(values = lw_values, guide = "none") +
    my.theme +
    xlab(xlab) + ylab(ylab) +
    theme(
      legend.position   = legend_pos,
      legend.title      = element_blank(),
      legend.background = element_rect(fill = "white", colour = "black"),
      legend.key        = element_rect(fill = NA, colour = NA)
    )
}

## invoke
#p1 <- power_over_time(
#  dat = dat,
#  y_col = Ah,
#  legend_pos = c(0.15, 0.80),
#  lw_values = transect_lw_2,
#  ylab = "Ah consumed"
#)
#print(p1)
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## plot kernel densities on log10 scale ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
density_stack <- function(dat,
                          x_col = "log_W",
                          transect_col = "transect",
                          fills = transect_fills,
                          labels = c(
                            "0" = "off transect",
                            "1" = "transect 1",
                            "2" = "transect 2"),
                          alpha = 0.85,
                          legend_pos = c(0.85, 0.80),
                          xlab = "Watts consumed",
                          ylab = "Density",
                          # KDE smoothing controls (histogram-binwidth analogue)
                          adjust = 1,
                          bw = NULL,
                          # x-axis formatting
                          back_transform = TRUE,
                          expand_mult = c(0.02, 0.08)) {
  x_col <- rlang::as_name(rlang::ensym(x_col))
  transect_col <- rlang::as_name(rlang::ensym(transect_col))
  
  dens_args <- list(position = "stack", alpha = alpha, adjust = adjust)
  if (!is.null(bw)) dens_args$bw <- bw
  
  p <- ggplot(dat, aes(x = .data[[x_col]], fill = .data[[transect_col]])) +
    do.call(geom_density, dens_args) +
    scale_fill_manual(values = fills, labels = labels) +
    my.theme +
    xlab(xlab) + ylab(ylab) +
    theme(
      axis.text.y   = element_blank(),
      axis.ticks.y  = element_blank(),
      legend.position   = legend_pos,
      legend.title      = element_blank(),
      legend.background = element_rect(fill = "white", colour = "black"),
      legend.key        = element_rect(fill = NA, colour = NA)  # no black boxes
    )
  
  if (back_transform) {
    p <- p + scale_x_continuous(
      labels = function(x) round(10^x),
      expand = expansion(mult = expand_mult)
    )
  } else {
    p <- p + scale_x_continuous(
      expand = expansion(mult = expand_mult)
    )
  }
  
  p
}


## invoke function to plot kernel density stack
#p2 <- density_stack(
#  dat = dat,
#  x_col = log_W,
#  legend_pos = c(0.85, 0.80),
#  xlab = "Power consumption (W)",
#  ylab = "Density",
#  adjust = 1
#)
#print(p2)
## END function to plot kernel density ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
