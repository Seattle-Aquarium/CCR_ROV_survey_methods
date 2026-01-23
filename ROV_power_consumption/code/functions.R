## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## Functions to process and analyze ROV power consumption
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~






## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## function to append multiple tlogs, maintain cumulative Wh 
append_tlogs <- function(dat1, dat2, wh_col = "Battery_Wh_used") {
  wh_end <- dat1[[wh_col]][max(which(!is.na(dat1[[wh_col]])))]
  dat2[[wh_col]] <- dat2[[wh_col]] + wh_end
  rbind(dat1, dat2)
}
#dat <- append_tlogs(dat, dat2)
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





## function to calculate Wh cumulative consumption ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
tabulate_wh <- function(dat,
                        wh_col = "Wh",
                        time_col = "Time",         
                        transect_col = "transect",
                        off_value = 0,
                        window_min = 60,
                        out_dir = "results",
                        out_file = "Wh_consumption.txt",
                        print_output = TRUE) {
  
  # ---- helpers ----
  parse_time_to_seconds_hhmmss <- function(x) {
    # Accepts "HH:MM:SS" and allows fractional seconds (SS can be "06.5")
    x <- trimws(as.character(x))
    parts <- strsplit(x, ":", fixed = TRUE)
    
    vapply(parts, function(p) {
      p <- p[p != ""]
      if (length(p) != 3L) return(NA_real_)
      hh <- suppressWarnings(as.numeric(p[1]))
      mm <- suppressWarnings(as.numeric(p[2]))
      ss <- suppressWarnings(as.numeric(p[3]))  # may be fractional
      if (anyNA(c(hh, mm, ss))) return(NA_real_)
      hh * 3600 + mm * 60 + ss
    }, numeric(1))
  }
  
  fmt_num <- function(x, digits = 3) if (is.finite(x)) sprintf(paste0("%.", digits, "f"), x) else "NA"
  
  elapsed_minutes <- function(t_start, t_end) {
    if (!is.finite(t_start) || !is.finite(t_end)) return(NA_real_)
    dt <- t_end - t_start
    if (is.finite(dt) && dt < 0) dt <- dt + 24 * 3600  # midnight rollover
    dt / 60
  }
  
  # ---- validate columns ----
  needed <- c(wh_col, time_col, transect_col)
  missing_cols <- setdiff(needed, names(dat))
  if (length(missing_cols) > 0) stop("Missing required column(s): ", paste(missing_cols, collapse = ", "))
  
  dir.create(out_dir, showWarnings = FALSE, recursive = TRUE)
  
  # Parse time once; preserve row order
  dat$.t_seconds <- parse_time_to_seconds_hhmmss(dat[[time_col]])
  
  # ---- output header ----
  lines <- character(0)
  lines <- c(lines, "Wh cumulative consumption summary")
  lines <- c(lines, paste0("Generated: ", format(Sys.time(), "%Y-%m-%d %H:%M:%S")))
  lines <- c(lines, paste0("Time column: ", time_col, " (HH:MM:SS)"))
  lines <- c(lines, paste0("Window for normalized energy: ", window_min, " min"))
  lines <- c(lines, "")
  
  tr <- dat[[transect_col]]
  
  # ============================================================
  # A) On-transect summaries by transect ID (transect != 0)
  # ============================================================
  on_idx <- !is.na(tr) & tr != off_value
  dat_on <- dat[on_idx, , drop = FALSE]
  
  if (nrow(dat_on) == 0) {
    lines <- c(lines, "No on-transect rows found (transect != off_value).", "")
  } else {
    transect_ids <- unique(dat_on[[transect_col]])
    transect_ids <- transect_ids[order(as.character(transect_ids))]
    
    lines <- c(lines, "On-transect (by transect ID)")
    lines <- c(lines, "----------------------------------------")
    
    for (tid in transect_ids) {
      d <- dat_on[dat_on[[transect_col]] == tid, , drop = FALSE]
      
      wh_vec <- as.numeric(d[[wh_col]])
      wh_vec <- wh_vec[!is.na(wh_vec)]
      total_wh <- if (length(wh_vec) >= 2) tail(wh_vec, 1) - wh_vec[1] else NA_real_
      
      tvec <- d$.t_seconds
      tvec <- tvec[!is.na(tvec)]
      el_min <- if (length(tvec) >= 2) elapsed_minutes(tvec[1], tvec[length(tvec)]) else NA_real_
      
      wh_per_min <- if (is.finite(total_wh) && is.finite(el_min) && el_min > 0) total_wh / el_min else NA_real_
      wh_per_window <- if (is.finite(wh_per_min)) wh_per_min * window_min else NA_real_
      
      start_time <- as.character(d[[time_col]][1])
      end_time   <- as.character(d[[time_col]][nrow(d)])
      
      lines <- c(lines, paste0("Transect: transect ", as.character(tid)))
      lines <- c(lines, paste0("  Segment start time: ", start_time))
      lines <- c(lines, paste0("  Segment end time:   ", end_time))
      lines <- c(lines, paste0("  Rows: ", nrow(d)))
      lines <- c(lines, paste0("  Total Wh (end - start): ", fmt_num(total_wh, 3)))
      lines <- c(lines, paste0("  Elapsed time (min): ", fmt_num(el_min, 2)))
      lines <- c(lines, paste0("  Wh per min: ", fmt_num(wh_per_min, 4)))
      lines <- c(lines, paste0("  Wh per ", window_min, " min: ", fmt_num(wh_per_window, 3)))
      lines <- c(lines, "")
    }
  }
  
  # ============================================================
  # B) Off-transect summaries by contiguous segment (transect == 0)
  # ============================================================
  off_idx <- !is.na(tr) & tr == off_value
  
  lines <- c(lines, "")
  lines <- c(lines, "Off-transect (transect == 0) by contiguous segment")
  lines <- c(lines, "----------------------------------------")
  
  if (!any(off_idx, na.rm = TRUE)) {
    lines <- c(lines, "No off-transect rows found (transect == off_value).", "")
  } else {
    rle_off <- rle(off_idx)
    run_lengths <- rle_off$lengths
    run_values  <- rle_off$values
    
    run_ends <- cumsum(run_lengths)
    run_starts <- run_ends - run_lengths + 1
    off_runs <- which(run_values)  # TRUE runs
    
    seg_tot_wh <- numeric(0)
    seg_tot_min <- numeric(0)
    
    seg_n <- 0L
    for (k in off_runs) {
      seg_n <- seg_n + 1L
      idx <- run_starts[k]:run_ends[k]
      d <- dat[idx, , drop = FALSE]
      
      wh_vec <- as.numeric(d[[wh_col]])
      wh_vec <- wh_vec[!is.na(wh_vec)]
      total_wh <- if (length(wh_vec) >= 2) tail(wh_vec, 1) - wh_vec[1] else NA_real_
      
      tvec <- d$.t_seconds
      tvec <- tvec[!is.na(tvec)]
      el_min <- if (length(tvec) >= 2) elapsed_minutes(tvec[1], tvec[length(tvec)]) else NA_real_
      
      wh_per_min <- if (is.finite(total_wh) && is.finite(el_min) && el_min > 0) total_wh / el_min else NA_real_
      wh_per_window <- if (is.finite(wh_per_min)) wh_per_min * window_min else NA_real_
      
      start_time <- as.character(d[[time_col]][1])
      end_time   <- as.character(d[[time_col]][nrow(d)])
      
      seg_tot_wh <- c(seg_tot_wh, total_wh)
      seg_tot_min <- c(seg_tot_min, el_min)
      
      lines <- c(lines, paste0("Off segment ", seg_n))
      lines <- c(lines, paste0("  Segment start time: ", start_time))
      lines <- c(lines, paste0("  Segment end time:   ", end_time))
      lines <- c(lines, paste0("  Rows: ", nrow(d)))
      lines <- c(lines, paste0("  Total Wh (end - start): ", fmt_num(total_wh, 3)))
      lines <- c(lines, paste0("  Elapsed time (min): ", fmt_num(el_min, 2)))
      lines <- c(lines, paste0("  Wh per min: ", fmt_num(wh_per_min, 4)))
      lines <- c(lines, paste0("  Wh per ", window_min, " min: ", fmt_num(wh_per_window, 3)))
      lines <- c(lines, "")
    }
    
    # Combined off-transect (sum across segments)
    total_off_wh <- sum(seg_tot_wh, na.rm = TRUE)
    total_off_min <- sum(seg_tot_min, na.rm = TRUE)
    
    off_wh_per_min <- if (is.finite(total_off_wh) && is.finite(total_off_min) && total_off_min > 0) {
      total_off_wh / total_off_min
    } else NA_real_
    
    off_wh_per_window <- if (is.finite(off_wh_per_min)) off_wh_per_min * window_min else NA_real_
    
    lines <- c(lines, "Off-transect combined (all off segments)")
    lines <- c(lines, paste0("  Segments: ", length(off_runs)))
    lines <- c(lines, paste0("  Total Wh (sum of segments): ", fmt_num(total_off_wh, 3)))
    lines <- c(lines, paste0("  Total time (min, sum of segments): ", fmt_num(total_off_min, 2)))
    lines <- c(lines, paste0("  Wh per min: ", fmt_num(off_wh_per_min, 4)))
    lines <- c(lines, paste0("  Wh per ", window_min, " min: ", fmt_num(off_wh_per_window, 3)))
    lines <- c(lines, "")
  }
  
  # ---- write to disk ----
  txt <- paste(lines, collapse = "\n")
  writeLines(txt, file.path(out_dir, out_file))
  if (print_output) cat(txt, "\n")
  invisible(txt)
}
## END functino to calculate cumulative Wh consumption ~~~~~~~~~~~~~~~~~~~~~~~~~





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
                            "2" = "transect 2",
                            "3" = "transect 3"),
                          alpha = 0.85,
                          legend_pos = c(0.85, 0.80),
                          xlab = "Watts consumed",
                          ylab = "Density",
                          # KDE smoothing controls (histogram-binwidth analogue)
                          adjust = 1,
                          bw = NULL,
                          # x-axis formatting
                          back_transform = TRUE,
                          expand_mult = c(0.02, 0.15)) {
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
## function to save figure ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
save_fig <- function(plot,
                     filename,
                     subfolder,
                     width  = 8,
                     height = 5,
                     dpi    = 300,
                     bg     = "white") {
  
  out_dir <- file.path("figs", subfolder)
  if (!dir.exists(out_dir)) dir.create(out_dir, recursive = TRUE)
  
  pdf_file <- file.path(out_dir, paste0(filename, ".pdf"))
  png_file <- file.path(out_dir, paste0(filename, ".png"))
  
  safe_off <- function() {
    if (grDevices::dev.cur() > 1) grDevices::dev.off()
  }
  
  ## PDF (vector)
  grDevices::cairo_pdf(pdf_file, width = width, height = height, bg = bg)
  on.exit(safe_off(), add = TRUE)
  print(plot)
  safe_off()
  
  ## PNG (raster)
  grDevices::png(png_file,
                 width = width, height = height,
                 units = "in", res = dpi,
                 type = "cairo-png",
                 bg = bg)
  on.exit(safe_off(), add = TRUE)
  print(plot)
  safe_off()
  
  invisible(list(pdf = pdf_file, png = png_file))
}


#save_fig(
#  p7, 
#  filename = "Wh_across_time", 
#  subfolder = "V3", 
#  width = 9, 
#  height = 4, 
#  dpi = 600)

## END function to save figs ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~





## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## END of script ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
