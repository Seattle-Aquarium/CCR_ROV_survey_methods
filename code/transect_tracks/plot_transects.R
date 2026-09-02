## ---------------------------------------------------------------------------
## plot_transects.R
##
## Reads every transect CSV in this folder, plots the lat/lon track from each
## file on a single interactive Leaflet map (one color per transect, with a
## legend), and adds a scale bar. Saves the result as a standalone HTML file
## and opens it in your browser.
##
## Expects each CSV to have "lat" and "lon" columns (decimal degrees).
## ---------------------------------------------------------------------------

library(readr)
library(dplyr)
library(leaflet)
library(htmlwidgets)
library(scales)

## ---- 1. Settings -----------------------------------------------------------

# Folder containing the transect CSVs.
# By default this assumes your R working directory is already set to the
# folder with the CSVs (e.g. in RStudio: Session > Set Working Directory >
# To Source File Location). Otherwise, replace "." with a full path, e.g.
# "C:/Users/randellz/OneDrive - Seattle Aquarium/Desktop/transect_tracks"
folder_path <- "."

# Only pick up files matching this pattern -- adjust if needed
file_pattern <- "\\.csv$"

output_html <- file.path(folder_path, "transect_tracks_map.html")

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

## ---- 3. Assign one color per transect --------------------------------------

transect_names <- levels(tracks$transect)
transect_colors <- scales::hue_pal()(length(transect_names))
pal <- colorFactor(palette = transect_colors, domain = transect_names)

## ---- 4. Build the Leaflet map ----------------------------------------------

map <- leaflet() %>%
  addProviderTiles(providers$Esri.WorldImagery, group = "Satellite") %>%
  addProviderTiles(providers$OpenStreetMap, group = "Street map") %>%
  addLayersControl(
    baseGroups = c("Satellite", "Street map"),
    options = layersControlOptions(collapsed = FALSE)
  )

for (tr in transect_names) {
  sub <- tracks %>% filter(transect == tr) %>% arrange(row_number())

  map <- map %>%
    addPolylines(
      data = sub, lng = ~lon, lat = ~lat,
      color = pal(tr), weight = 3, opacity = 0.9,
      group = tr, label = tr
    ) %>%
    addCircleMarkers(
      data = sub[1, ], lng = ~lon, lat = ~lat,
      color = pal(tr), radius = 5, fillOpacity = 1, stroke = FALSE,
      label = paste(tr, "- start")
    )
}

map <- map %>%
  addLegend(
    position = "bottomright",
    pal = pal, values = transect_names,
    title = "Transect", opacity = 1
  ) %>%
  addScaleBar(position = "bottomleft", options = scaleBarOptions(imperial = TRUE))

## ---- 5. Save and open the map -----------------------------------------------

saveWidget(map, file = output_html, selfcontained = FALSE)
message("Map saved to: ", normalizePath(output_html))

if (interactive()) {
  print(map)
} else {
  utils::browseURL(normalizePath(output_html))
}
