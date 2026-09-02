## ---------------------------------------------------------------------------
## plot_subfig_d.R
##
## Standalone panel (d): raw acoustic-GPS (UGPS) vs. raw DVL vs. fused EKF
## tracks, colored by data source (not by transect). Reads every CSV in
## subfig_d/ and saves subfig_d.png / .pdf.
##
## For the combined 2x2 figure used in the manuscript, see
## plot_transects_combined.R -- it reuses the same panel_builders.R logic so
## this standalone version and the combined one can't drift apart.
##
## Expects each CSV to have "Latitude"/"Longitude" (raw UGPS), "DVLlat"/
## "DVLlon" (raw DVL), and "EKFlat"/"EKFlon" (fused) columns (decimal
## degrees).
## ---------------------------------------------------------------------------

source("panel_builders.R")

sources <- list(
  GPS = c("Latitude", "Longitude"),
  DVL = c("DVLlat", "DVLlon"),
  EKF = c("EKFlat", "EKFlon")
)
source_colors <- c(GPS = "black", DVL = "blue", EKF = "red")
source_labels <- c(GPS = "UGPS", DVL = "DVL", EKF = "EKF")

res <- build_source_colored_panel("subfig_d", sources, source_colors, source_labels)
fig <- add_scale_and_north_arrow(res$fig)

save_figure(fig, res$xlim, res$ylim, "subfig_d")

if (interactive()) print(fig)
