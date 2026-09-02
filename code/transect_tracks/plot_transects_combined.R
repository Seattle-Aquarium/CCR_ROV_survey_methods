## ---------------------------------------------------------------------------
## plot_transects_combined.R
##
## Combines all four manuscript panels (subfig_a/b/c/d) into a single 2x2
## figure: one shared "Longitude" title beneath and one shared "Latitude"
## title to the left of the whole grid, "(a)".."(d)" tags in the upper-left
## of each panel, tight spacing between panels, and every panel forced to
## the same fixed aspect ratio (still true-to-scale -- see
## fixed_aspect_extent() in common_figure_style.R) so the four cells line up
## cleanly.
##
## Saves ONE file: transects_combined.pdf / .png. This is the file to
## \includegraphics in LaTeX -- the standalone plot_subfig_*.R scripts are
## still there if you ever need an individual panel on its own.
## ---------------------------------------------------------------------------

source("panel_builders.R")
library(gridExtra)
library(grid)

target_aspect <- 1.1  # shared lat/lon panel aspect ratio (all 4 panels are naturally close to this already)

## ---- Build the four panels (no scale bar/north arrow/tag yet) --------------

panel_a <- build_transect_colored_panel("subfig_a", target_aspect = target_aspect)
panel_b <- build_transect_colored_panel("subfig_b", target_aspect = target_aspect)

sources_c <- list(GPS = c("Latitude", "Longitude"), DVL = c("DVLlat", "DVLlon"))
panel_c <- build_source_colored_panel(
  "subfig_c", sources_c,
  c(GPS = "black", DVL = "blue"),
  c(GPS = "UGPS", DVL = "DVL"),
  target_aspect = target_aspect
)

sources_d <- list(GPS = c("Latitude", "Longitude"), DVL = c("DVLlat", "DVLlon"), EKF = c("EKFlat", "EKFlon"))
panel_d <- build_source_colored_panel(
  "subfig_d", sources_d,
  c(GPS = "black", DVL = "blue", EKF = "red"),
  c(GPS = "UGPS", DVL = "DVL", EKF = "EKF"),
  target_aspect = target_aspect
)

## ---- Add scale bar/north arrow + the (a)-(d) tag to each; drop each -------
## panel's own "Longitude"/"Latitude" title text (a single shared pair is
## added below instead) and tighten the per-panel margin. patchwork's
## built-in axis_titles/axes collection doesn't play well with rotated
## axis.text.y (a known limitation), and nesting a combined patchwork object
## inside gridExtra::arrangeGrob renders blank -- so the whole 2x2 grid and
## its shared titles are built in one flat gridExtra::arrangeGrob call
## instead.

strip_titles <- function(fig, tag) {
  add_panel_tag(add_scale_and_north_arrow(fig), tag) +
    labs(x = NULL, y = NULL) +
    theme(plot.margin = margin(t = 4, r = 20, b = 4, l = 4))
}

fig_a <- strip_titles(panel_a$fig, "(a)")
fig_b <- strip_titles(panel_b$fig, "(b)")
fig_c <- strip_titles(panel_c$fig, "(c)")
fig_d <- strip_titles(panel_d$fig, "(d)")

## ---- Combine into a tight 2x2 grid with one shared axis title pair ---------

combined <- arrangeGrob(
  fig_a, fig_b, fig_c, fig_d,
  ncol = 2,
  left = textGrob("Latitude", rot = 90, gp = gpar(fontsize = 16)),
  bottom = textGrob("Longitude", gp = gpar(fontsize = 16))
)

## ---- Save --------------------------------------------------------------------
## Each cell must be sized so its own rotated tick labels have room (the
## same per-panel width/height accounting save_figure() uses for a
## standalone panel), or labels compress and overlap -- then the full
## canvas is just 2x that cell, plus a bit for the shared axis titles.
## (Rotated axis text below ~5.7in of cell width starts visibly crowding at
## this font size -- 6.2in keeps a safety margin above that threshold.)

cell_width_in <- 6.2
axis_margin_w_in <- 0.55
axis_margin_h_in <- 0.8
cell_height_in <- (cell_width_in - axis_margin_w_in) * target_aspect + axis_margin_h_in

output_stub <- "transects_combined"
fig_width <- 2 * cell_width_in + 0.4   # + shared "Latitude" title strip
fig_height <- 2 * cell_height_in + 0.4 # + shared "Longitude" title strip

ggsave(paste0(output_stub, ".png"), combined, width = fig_width, height = fig_height, dpi = 300, bg = "white")
ggsave(paste0(output_stub, ".pdf"), combined, width = fig_width, height = fig_height, bg = "white")

message("Saved: ", normalizePath(paste0(output_stub, ".pdf")))

if (interactive()) grid.draw(combined)
