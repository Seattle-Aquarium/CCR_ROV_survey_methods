## ---------------------------------------------------------------------------
## panel_builders.R
##
## Builds the two kinds of track panel used across this project:
##   - build_transect_colored_panel(): one color per transect (subfig_a/b)
##   - build_source_colored_panel(): one color per data source, e.g.
##     UGPS/DVL/EKF, across all transects in a folder (subfig_c/d)
##
## Both return list(fig, xlim, ylim) -- fig has NO scale bar/north arrow/tag
## yet (see add_scale_and_north_arrow() / add_panel_tag() in
## common_figure_style.R) so callers can add those, and can add or strip
## axis titles, however suits standalone vs. combined output.
##
## Sourced by plot_subfig_a.R .. plot_subfig_d.R and
## plot_transects_combined.R -- single source of truth for how a panel's
## data gets read and drawn, so standalone and combined figures can't drift
## apart.
## ---------------------------------------------------------------------------

source("common_figure_style.R")

build_transect_colored_panel <- function(folder_path, pad_fraction = 0.08, target_aspect = NULL) {
  csv_files <- list.files(folder_path, pattern = "\\.csv$", full.names = TRUE, recursive = TRUE)
  if (length(csv_files) == 0) stop("No CSV files found in: ", folder_path)

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
  if (nrow(tracks) == 0) stop("No valid lat/lon data found in any CSV.")
  tracks$transect <- factor(tracks$transect, levels = unique(tracks$transect))

  points_sf <- st_as_sf(tracks, coords = c("lon", "lat"), crs = 4326, remove = FALSE)

  lines_sf <- points_sf %>%
    arrange(transect, point_order) %>%
    group_by(transect) %>%
    summarise(do_union = FALSE, .groups = "drop") %>%
    st_cast("LINESTRING")

  ext <- fixed_aspect_extent(points_sf, pad_fraction, target_aspect)

  transect_names <- levels(tracks$transect)
  n_transects <- length(transect_names)
  transect_colors <- if (n_transects <= 8) {
    brewer.pal(max(n_transects, 3), "Dark2")[seq_len(n_transects)]
  } else {
    grDevices::colorRampPalette(brewer.pal(8, "Dark2"))(n_transects)
  }
  names(transect_colors) <- transect_names
  transect_labels <- short_transect_label(transect_names)

  fig <- ggplot() +
    geom_sf(data = lines_sf, aes(color = transect), linewidth = 0.5, alpha = 0.8) +
    geom_sf(data = points_sf, aes(color = transect), size = 0.6, alpha = 0.5) +
    coord_sf(xlim = ext$xlim, ylim = ext$ylim, expand = FALSE) +
    scale_color_manual(values = transect_colors, name = "Transect", labels = transect_labels) +
    nice_coord_axes() +
    labs(x = "Longitude", y = "Latitude") +
    manuscript_theme() +
    guides(color = guide_legend(override.aes = list(linewidth = 1.5, size = 2, alpha = 1)))

  list(fig = fig, xlim = ext$xlim, ylim = ext$ylim)
}

build_source_colored_panel <- function(folder_path, sources, source_colors, source_labels,
                                        pad_fraction = 0.08, target_aspect = NULL) {
  csv_files <- list.files(folder_path, pattern = "\\.csv$", full.names = TRUE, recursive = TRUE)
  if (length(csv_files) == 0) stop("No CSV files found in: ", folder_path)

  tracks <- bind_rows(lapply(csv_files, read_multi_source_tracks, sources = sources))
  if (nrow(tracks) == 0) stop("No valid data found in any CSV.")

  tracks$transect <- factor(tracks$transect, levels = unique(tracks$transect))
  tracks$source <- factor(tracks$source, levels = names(sources))

  points_sf <- st_as_sf(tracks, coords = c("lon", "lat"), crs = 4326, remove = FALSE)

  lines_sf <- points_sf %>%
    arrange(transect, source, point_order) %>%
    group_by(transect, source) %>%
    summarise(do_union = FALSE, .groups = "drop") %>%
    st_cast("LINESTRING")

  ext <- fixed_aspect_extent(points_sf, pad_fraction, target_aspect)

  fig <- ggplot() +
    geom_sf(data = lines_sf, aes(color = source), linewidth = 0.5, alpha = 0.8) +
    geom_sf(data = points_sf, aes(color = source), size = 0.6, alpha = 0.5) +
    coord_sf(xlim = ext$xlim, ylim = ext$ylim, expand = FALSE) +
    scale_color_manual(values = source_colors, name = NULL, labels = source_labels) +
    nice_coord_axes() +
    labs(x = "Longitude", y = "Latitude") +
    manuscript_theme() +
    guides(color = guide_legend(override.aes = list(linewidth = 1.5, size = 2, alpha = 1)))

  list(fig = fig, xlim = ext$xlim, ylim = ext$ylim)
}
