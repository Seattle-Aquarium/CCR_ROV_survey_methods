## ---------------------------------------------------------------------------
## plot_subfig_c.R
##
## Standalone panel (c): raw acoustic-GPS (UGPS) tracks vs. raw DVL tracks,
## colored by data source (not by transect). Reads every CSV in subfig_c/
## and saves subfig_c.png / .pdf.
##
## For the combined 2x2 figure used in the manuscript, see
## plot_transects_combined.R -- it reuses the same panel_builders.R logic so
## this standalone version and the combined one can't drift apart.
##
## Expects each CSV to have "Latitude"/"Longitude" (raw UGPS) and
## "DVLlat"/"DVLlon" (raw DVL) columns (decimal degrees).
## ---------------------------------------------------------------------------

source("panel_builders.R")

sources <- list(
  GPS = c("Latitude", "Longitude"),
  DVL = c("DVLlat", "DVLlon")
)
source_colors <- c(GPS = "black", DVL = "blue")
source_labels <- c(GPS = "UGPS", DVL = "DVL")

res <- build_source_colored_panel("subfig_c", sources, source_colors, source_labels)
fig <- add_scale_and_north_arrow(res$fig)

save_figure(fig, res$xlim, res$ylim, "subfig_c")

if (interactive()) print(fig)
