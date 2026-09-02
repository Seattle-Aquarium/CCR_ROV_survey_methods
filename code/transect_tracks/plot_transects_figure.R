## ---------------------------------------------------------------------------
## plot_transects_figure.R
##
## Static, publication-style figure (not an interactive Leaflet map) showing
## the lat/lon track from every transect CSV in this folder, colored by
## transect, zoomed tightly to the extent of the tracks themselves, with a
## proper legend and a geographically-correct scale bar.
##
## Useful for e.g. illustrating GPS/acoustic-positioning noise in a
## manuscript figure.
##
## Expects each CSV to have "lat" and "lon" columns (decimal degrees).
## ---------------------------------------------------------------------------

library(readr)
library(dplyr)
library(sf)
library(ggplot2)
library(ggspatial)
library(RColorBrewer)

## ---- 1. Settings -----------------------------------------------------------

# Folder containing the transect CSVs.
# By default this assumes your R working directory is already set to the
# folder with the CSVs (e.g. in RStudio: Session > Set Working Directory >
# To Source File Location). Otherwise, replace "." with a full path, e.g.
# "C:/Users/randellz/OneDrive - Seattle Aquarium/Desktop/transect_tracks"
folder_path <- "."

file_pattern <- "\\.csv$"

# Fraction of the track extent to pad around the edges of the plot
pad_fraction <- 0.08

output_png <- file.path(folder_path, "transect_tracks_figure.png")
output_pdf <- file.path(folder_path, "transect_tracks_figure.pdf")

## ---- 2. Read and combine all transect CSVs ---------------------------------

csv_files <- list.files(folder_path, pattern = file_pattern, full.names = TRUE, recursive = TRUE)

if (length(csv_files) == 0) {
  stop("No CSV files found in: ", folder_path)
}

read_transect <- function(path) {
  df <- suppressMessages(read_csv(path, show_col_types = FALSE))

  if (!all(c("lat", "lon") %in% names(df))) {
    warning("Skipping '", basename(path), "': no 'lat'/'lon' columns found.")
    return(NULL)
  }

  df %>%
    transmute(
      transect = tools::file_path_sans_ext(basename(path)),
      point_order = row_number(),
      lat = as.numeric(lat),
      lon = as.numeric(lon)
    ) %>%
    filter(!is.na(lat), !is.na(lon))
}

tracks <- bind_rows(lapply(csv_files, read_transect))

if (nrow(tracks) == 0) {
  stop("No valid lat/lon data found in any CSV.")
}

tracks$transect <- factor(tracks$transect, levels = unique(tracks$transect))

## ---- 3. Build sf points + lines for each transect --------------------------

points_sf <- st_as_sf(tracks, coords = c("lon", "lat"), crs = 4326, remove = FALSE)

lines_sf <- points_sf %>%
  arrange(transect, point_order) %>%
  group_by(transect) %>%
  summarise(do_union = FALSE) %>%
  st_cast("LINESTRING")

## ---- 4. Zoom extent: bounding box of the tracks, with a small pad ----------

bbox <- st_bbox(points_sf)
lon_pad <- diff(bbox[c("xmin", "xmax")]) * pad_fraction
lat_pad <- diff(bbox[c("ymin", "ymax")]) * pad_fraction

xlim <- c(bbox["xmin"] - lon_pad, bbox["xmax"] + lon_pad)
ylim <- c(bbox["ymin"] - lat_pad, bbox["ymax"] + lat_pad)

## ---- 5. Colors (colorblind-safe) --------------------------------------------

transect_names <- levels(tracks$transect)
n_transects <- length(transect_names)
transect_colors <- if (n_transects <= 8) {
  brewer.pal(max(n_transects, 3), "Dark2")[seq_len(n_transects)]
} else {
  grDevices::colorRampPalette(brewer.pal(8, "Dark2"))(n_transects)
}
names(transect_colors) <- transect_names

# Short legend labels, e.g. "2022_10_06_S1_T1" -> "T1". Falls back to the
# full transect name if it doesn't end in "T<number>".
short_transect_label <- function(x) {
  m <- regmatches(x, regexpr("T[0-9]+$", x))
  ifelse(nchar(m) > 0, m, x)
}
transect_labels <- short_transect_label(transect_names)

## ---- 6. Build the figure -----------------------------------------------------

fig <- ggplot() +
  geom_sf(data = lines_sf, aes(color = transect), linewidth = 0.5, alpha = 0.8) +
  geom_sf(data = points_sf, aes(color = transect), size = 0.6, alpha = 0.5) +
  coord_sf(xlim = xlim, ylim = ylim, expand = FALSE) +
  scale_color_manual(values = transect_colors, name = "Transect", labels = transect_labels) +
  annotation_scale(
    location = "bl", width_hint = 0.3,
    bar_cols = c("black", "white"), text_cex = 0.8
  ) +
  annotation_north_arrow(
    location = "br", which_north = "true",
    style = north_arrow_minimal(text_size = 8),
    height = unit(0.9, "cm"), width = unit(0.9, "cm")
  ) +
  labs(x = "Longitude", y = "Latitude") +
  theme_bw(base_size = 12) +
  theme(
    panel.grid = element_line(color = "grey90"),
    axis.title = element_text(size = 13),
    axis.text = element_text(size = 11),
    # Inset legend, anchored to the top-right corner of the panel
    legend.position = c(0.98, 0.98),
    legend.justification = c(1, 1),
    legend.background = element_rect(fill = scales::alpha("white", 0.75), color = "grey50", linewidth = 0.3),
    legend.margin = margin(4, 6, 4, 6),
    legend.key = element_blank(),
    legend.key.size = unit(0.4, "cm"),
    legend.title = element_text(size = 11),
    legend.text = element_text(size = 10),
    plot.margin = margin(t = 5.5, r = 40, b = 5.5, l = 5.5)
  ) +
  guides(color = guide_legend(override.aes = list(linewidth = 1.5, size = 2, alpha = 1)))

## ---- 7. Save the figure -------------------------------------------------------

# Rough aspect ratio match so the panel isn't mostly whitespace, accounting
# for longitude/latitude not being equal distances (except near the equator).
# The legend is now an inset overlay (no separate column), so only the axis
# titles/labels need to be subtracted out before matching the panel's height
# to its true geographic aspect ratio. Sized as roughly one panel of a
# 3-panel, 1-row figure -- adjust fig_width for your final layout.
mean_lat_rad <- mean(bbox[c("ymin", "ymax")]) * pi / 180
lon_span <- diff(xlim) * cos(mean_lat_rad)
lat_span <- diff(ylim)

fig_width <- 6.5
axis_margin_w_in <- 1.0     # y-axis title + tick labels
axis_margin_h_in <- 0.8     # x-axis title + tick labels

panel_width_in <- fig_width - axis_margin_w_in
fig_height <- panel_width_in * (lat_span / lon_span) + axis_margin_h_in

ggsave(output_png, fig, width = fig_width, height = fig_height, dpi = 300)
ggsave(output_pdf, fig, width = fig_width, height = fig_height)

message("Figure saved to:\n  ", normalizePath(output_png), "\n  ", normalizePath(output_pdf))

if (interactive()) print(fig)
