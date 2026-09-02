## ---------------------------------------------------------------------------
## plot_subfig_a.R
##
## Standalone panel (a): raw acoustic-GPS tracks only, one color per
## transect. Reads every CSV in subfig_a/ and saves subfig_a.png / .pdf.
##
## For the combined 2x2 figure used in the manuscript, see
## plot_transects_combined.R -- it reuses the same panel_builders.R logic so
## this standalone version and the combined one can't drift apart.
##
## Expects each CSV to have "lat" and "lon" columns (decimal degrees).
## ---------------------------------------------------------------------------

source("panel_builders.R")

res <- build_transect_colored_panel("subfig_a")
fig <- add_scale_and_north_arrow(res$fig)

save_figure(fig, res$xlim, res$ylim, "subfig_a")

if (interactive()) print(fig)
