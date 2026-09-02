## ---------------------------------------------------------------------------
## plot_subfig_b.R
##
## Standalone panel (b): raw acoustic-GPS tracks only, one color per
## transect. Reads every CSV in subfig_b/ and saves subfig_b.png / .pdf.
##
## For the combined 2x2 figure used in the manuscript, see
## plot_transects_combined.R -- it reuses the same panel_builders.R logic so
## this standalone version and the combined one can't drift apart.
##
## Expects each CSV to have "lat" and "lon" columns (decimal degrees).
## ---------------------------------------------------------------------------

source("panel_builders.R")

res <- build_transect_colored_panel("subfig_b")
fig <- add_scale_and_north_arrow(res$fig)

save_figure(fig, res$xlim, res$ylim, "subfig_b")

if (interactive()) print(fig)
