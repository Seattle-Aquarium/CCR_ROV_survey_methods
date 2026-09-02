## ---------------------------------------------------------------------------
## common_figure_style.R
##
## Shared theme, scale bar/north arrow placement, sizing/saving, and CSV
## helpers used by panel_builders.R (which in turn is used by
## plot_subfig_a.R .. plot_subfig_d.R and plot_transects_combined.R) so every
## panel -- standalone or combined -- stays visually consistent (axis text
## size, legend style, line/point styling, scale bar, north arrow).
##
## Not meant to be run directly -- sourced by panel_builders.R.
## ---------------------------------------------------------------------------

library(readr)
library(dplyr)
library(sf)
library(ggplot2)
library(ggspatial)
library(RColorBrewer)
library(scales)

## Shared ggplot theme -----------------------------------------------------

manuscript_theme <- function() {
  theme_bw(base_size = 13) +
    theme(
      panel.grid = element_line(color = "grey90"),
      axis.title = element_text(size = 15),
      axis.text = element_text(size = 12.5),
      # Vertical latitude tick labels take up far less horizontal margin
      # than e.g. "47.6310°N" printed sideways-on, leaving more room
      # for the panel itself once the figures are laid out 2x2 instead of
      # 1x3.
      axis.text.y = element_text(angle = 90, hjust = 0.5, vjust = 0.5),
      legend.position = c(0.98, 0.98),
      legend.justification = c(1, 1),
      legend.background = element_rect(fill = alpha("white", 0.75), color = "grey50", linewidth = 0.3),
      legend.margin = margin(4, 6, 4, 6),
      legend.key = element_blank(),
      legend.key.size = unit(0.45, "cm"),
      legend.title = element_text(size = 13),
      legend.text = element_text(size = 12),
      plot.margin = margin(t = 5.5, r = 40, b = 5.5, l = 5.5)
    )
}

## Zoom extent (xlim/ylim) around a set of points, padded out to an exact
## target aspect ratio (lat_span / lon_span, cos-latitude corrected) if one
## is given. This lets several panels share one fixed panel shape for a tidy
## grid layout WITHOUT distorting anything -- coord_sf still renders true
## geographic scale; we're only choosing to show a bit more empty
## water/land on whichever axis is short of the target ratio, exactly the
## same way you'd zoom a map out slightly to fit a frame.

fixed_aspect_extent <- function(points_sf, pad_fraction = 0.08, target_aspect = NULL) {
  bbox <- st_bbox(points_sf)
  lon_pad <- diff(bbox[c("xmin", "xmax")]) * pad_fraction
  lat_pad <- diff(bbox[c("ymin", "ymax")]) * pad_fraction
  xlim <- c(bbox["xmin"] - lon_pad, bbox["xmax"] + lon_pad)
  ylim <- c(bbox["ymin"] - lat_pad, bbox["ymax"] + lat_pad)

  if (is.null(target_aspect)) return(list(xlim = xlim, ylim = ylim))

  mean_lat_rad <- mean(ylim) * pi / 180
  lon_span <- diff(xlim) * cos(mean_lat_rad)
  lat_span <- diff(ylim)
  current_aspect <- lat_span / lon_span

  if (current_aspect < target_aspect) {
    # panel would be relatively too wide -- pad north/south
    needed_lat_span <- target_aspect * lon_span
    extra <- (needed_lat_span - lat_span) / 2
    ylim <- c(ylim[1] - extra, ylim[2] + extra)
  } else if (current_aspect > target_aspect) {
    # panel would be relatively too tall -- pad east/west
    needed_lon_span <- lat_span / target_aspect
    extra_cos <- (needed_lon_span - lon_span) / 2
    xlim <- c(xlim[1] - extra_cos / cos(mean_lat_rad), xlim[2] + extra_cos / cos(mean_lat_rad))
  }

  list(xlim = xlim, ylim = ylim)
}

## Fewer, evenly-spaced axis breaks -- prevents crowded/overlapping tick
## labels on panels with a narrow lon or lat span (long decimal labels).

nice_coord_axes <- function(n_x = 4, n_y = 5) {
  list(
    scale_x_continuous(breaks = scales::breaks_pretty(n = n_x)),
    scale_y_continuous(breaks = scales::breaks_pretty(n = n_y))
  )
}

## Scale bar (bottom-left) + north arrow stacked directly above it ----------

add_scale_and_north_arrow <- function(fig) {
  fig +
    annotation_scale(
      location = "bl", width_hint = 0.3,
      bar_cols = c("black", "white"), text_cex = 0.8
    ) +
    annotation_north_arrow(
      location = "bl", which_north = "true",
      style = north_arrow_minimal(text_size = 8),
      height = unit(0.9, "cm"), width = unit(0.9, "cm"),
      pad_x = unit(0.2, "cm"), pad_y = unit(1.1, "cm")
    )
}

## In-panel "(a)"/"(b)"/... label, upper-left, for the combined figure -----

add_panel_tag <- function(fig, tag) {
  fig +
    labs(tag = tag) +
    theme(
      plot.tag = element_text(size = 15, face = "bold"),
      plot.tag.position = c(0.035, 0.975),
      plot.tag.location = "panel"
    )
}

## Save PNG (300 dpi, for quick viewing) + PDF (vector, for LaTeX) ---------
## Panel height is matched to the true geographic aspect ratio of xlim/ylim
## so tracks aren't stretched; axis_margin_*_in roughly account for axis
## titles/labels eating into the canvas without adding panel size.

save_figure <- function(fig, xlim, ylim, output_stub, fig_width = 6.5,
                         axis_margin_w_in = 0.55, axis_margin_h_in = 0.8) {
  mean_lat_rad <- mean(ylim) * pi / 180
  lon_span <- diff(xlim) * cos(mean_lat_rad)
  lat_span <- diff(ylim)

  panel_width_in <- fig_width - axis_margin_w_in
  fig_height <- panel_width_in * (lat_span / lon_span) + axis_margin_h_in

  png_path <- paste0(output_stub, ".png")
  pdf_path <- paste0(output_stub, ".pdf")

  ggsave(png_path, fig, width = fig_width, height = fig_height, dpi = 300)
  ggsave(pdf_path, fig, width = fig_width, height = fig_height)

  message("Saved: ", normalizePath(pdf_path))
  invisible(fig_height)
}

## Short legend labels, e.g. "2022_10_06_S1_T1" -> "T1". Falls back to the
## full transect name if it doesn't end in "T<number>".

short_transect_label <- function(x) {
  m <- regmatches(x, regexpr("T[0-9]+$", x))
  ifelse(nchar(m) > 0, m, x)
}

## Read one CSV, splitting it into one row-set per data "source" (e.g. raw
## GPS vs. DVL vs. EKF), based on a named list of lat/lon column pairs, e.g.
##   list(GPS = c("Latitude","Longitude"), DVL = c("DVLlat","DVLlon"))
## Rows with NA in that source's lat/lon are dropped (independently per
## source, so a missing EKF fix doesn't remove that row's GPS/DVL fix).

read_multi_source_tracks <- function(path, sources) {
  df <- suppressMessages(read_csv(path, show_col_types = FALSE))
  transect_id <- tools::file_path_sans_ext(basename(path))

  rows <- lapply(names(sources), function(src) {
    cols <- sources[[src]]
    if (!all(cols %in% names(df))) return(NULL)

    lat <- as.numeric(df[[cols[1]]])
    lon <- as.numeric(df[[cols[2]]])

    tibble(
      transect = transect_id,
      source = src,
      point_order = seq_along(lat),
      lat = lat,
      lon = lon
    ) %>%
      filter(!is.na(lat), !is.na(lon))
  })

  bind_rows(rows)
}
