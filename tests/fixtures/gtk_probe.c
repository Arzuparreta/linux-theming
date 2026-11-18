/* Test-only GTK 3 module, loaded with GTK_MODULES into an application running on
 * a private broadway display. After LT_PROBE_DELAY milliseconds it optionally
 * focuses each window, selects every file, opens the first menu-bar menu, then
 * writes each visible toplevel's widget tree (type, CSS name, state, classes and
 * computed colors) to $LT_PROBE_OUT/tree.txt, renders it to window-N.png and
 * exits. It reads the application's state and never writes settings. */
#include <gtk/gtk.h>
#include <stdio.h>
#include <stdlib.h>

static FILE *out;

static void color(GtkStyleContext *context, const char *property, char *buffer, size_t size)
{
  GdkRGBA *value = NULL;
  gtk_style_context_get (context, gtk_style_context_get_state (context), property, &value, NULL);
  if (value)
    {
      snprintf (buffer, size, "#%02x%02x%02x/%.2f", (int) (value->red * 255 + .5), (int) (value->green * 255 + .5),
                (int) (value->blue * 255 + .5), value->alpha);
      gdk_rgba_free (value);
    }
  else
    snprintf (buffer, size, "#000000/0.00");
}

static void walk (GtkWidget *widget, gpointer depth);

static void walk (GtkWidget *widget, gpointer depth)
{
  GtkStyleContext *context = gtk_widget_get_style_context (widget);
  GList *classes = gtk_style_context_list_classes (context);
  cairo_pattern_t *image = NULL;
  char background[32], foreground[32];

  color (context, "background-color", background, sizeof background);
  color (context, "color", foreground, sizeof foreground);
  gtk_style_context_get (context, gtk_style_context_get_state (context), "background-image", &image, NULL);
  fprintf (out, "%*s%s css=%s mapped=%d bg=%s fg=%s img=%d state=%x classes=", GPOINTER_TO_INT (depth) * 2, "",
           G_OBJECT_TYPE_NAME (widget), gtk_widget_class_get_css_name (GTK_WIDGET_GET_CLASS (widget)),
           gtk_widget_get_mapped (widget), background, foreground, image != NULL, gtk_style_context_get_state (context));
  if (image)
    cairo_pattern_destroy (image);
  for (GList *item = classes; item; item = item->next)
    fprintf (out, ".%s", (char *) item->data);
  g_list_free (classes);
  fprintf (out, "\n");
  if (GTK_IS_CONTAINER (widget))
    gtk_container_forall (GTK_CONTAINER (widget), walk, GINT_TO_POINTER (GPOINTER_TO_INT (depth) + 1));
}

static void select_files (GtkWidget *widget, gpointer data)
{
  const char *type = G_OBJECT_TYPE_NAME (widget);
  if (GTK_IS_TREE_VIEW (widget) && !g_str_equal (type, "ThunarShortcutsView") && !g_str_equal (type, "ThunarTreeView"))
    gtk_tree_selection_select_all (gtk_tree_view_get_selection (GTK_TREE_VIEW (widget)));
  else if (g_str_equal (type, "ExoIconView"))
    {
      gboolean handled;
      g_signal_emit_by_name (widget, "select-all", &handled);
    }
  if (GTK_IS_CONTAINER (widget))
    gtk_container_forall (GTK_CONTAINER (widget), select_files, data);
}

static GtkWidget *find (GtkWidget *widget, const char *type)
{
  GtkWidget *found = NULL;
  if (g_str_equal (G_OBJECT_TYPE_NAME (widget), type))
    return widget;
  if (GTK_IS_CONTAINER (widget))
    {
      GList *children = gtk_container_get_children (GTK_CONTAINER (widget));
      for (GList *item = children; item && !found; item = item->next)
        found = find (item->data, type);
      g_list_free (children);
    }
  return found;
}

static void focus (GtkWidget *widget)
{
  GdkWindow *window = gtk_widget_get_window (widget);
  GdkEvent *event;
  if (!window)
    return;
  event = gdk_event_new (GDK_FOCUS_CHANGE);
  event->focus_change.window = g_object_ref (window);
  event->focus_change.send_event = TRUE;
  event->focus_change.in = TRUE;
  gtk_widget_send_focus_change (widget, event);
  gdk_event_free (event);
}

static void settle (void)
{
  for (int i = 0; i < 50; i++)
    {
      while (gtk_events_pending ())
        gtk_main_iteration ();
      g_usleep (10000);
    }
}

static gboolean dump (gpointer data)
{
  char path[4096];
  int index = 0;
  GList *windows = gtk_window_list_toplevels ();

  for (GList *item = windows; item; item = item->next)
    {
      GtkWidget *window = item->data;
      if (!gtk_widget_get_visible (window))
        continue;
      if (g_getenv ("LT_PROBE_FOCUS"))
        focus (window);
      if (g_getenv ("LT_PROBE_SELECT"))
        select_files (window, NULL);
      if (g_getenv ("LT_PROBE_MENU") && g_str_equal (G_OBJECT_TYPE_NAME (window), "ThunarWindow"))
        {
          GtkWidget *bar = find (window, "GtkMenuBar");
          GList *menus = bar ? gtk_container_get_children (GTK_CONTAINER (bar)) : NULL;
          if (menus)
            {
              gtk_menu_shell_select_item (GTK_MENU_SHELL (bar), menus->data);
              gtk_menu_item_activate (GTK_MENU_ITEM (menus->data));
            }
          g_list_free (menus);
        }
    }
  g_list_free (windows);
  settle ();

  snprintf (path, sizeof path, "%s/tree.txt", g_getenv ("LT_PROBE_OUT"));
  out = fopen (path, "w");
  if (!out)
    exit (3);
  windows = gtk_window_list_toplevels ();
  for (GList *item = windows; item; item = item->next)
    {
      GtkWidget *window = item->data;
      GtkRequisition natural;
      GtkAllocation area = {0, 0, 0, 0};
      cairo_surface_t *surface;
      cairo_t *cr;
      gboolean main_window;

      if (!gtk_widget_get_visible (window))
        continue;
      main_window = g_str_equal (G_OBJECT_TYPE_NAME (window), "ThunarWindow");
      gtk_widget_get_preferred_size (window, NULL, &natural);
      area.width = main_window ? MAX (natural.width, 1000) : natural.width;
      area.height = main_window ? MAX (natural.height, 640) : natural.height;
      fprintf (out, "=== toplevel %d\n", index);
      walk (window, GINT_TO_POINTER (0));
      for (int i = 0; i < 5; i++)
        {
          gtk_widget_size_allocate (window, &area);
          while (gtk_events_pending ())
            gtk_main_iteration ();
        }
      gtk_widget_size_allocate (window, &area);
      surface = cairo_image_surface_create (CAIRO_FORMAT_ARGB32, area.width, area.height);
      cr = cairo_create (surface);
      gtk_widget_draw (window, cr);
      cairo_destroy (cr);
      snprintf (path, sizeof path, "%s/window-%d.png", g_getenv ("LT_PROBE_OUT"), index++);
      cairo_surface_write_to_png (surface, path);
      cairo_surface_destroy (surface);
    }
  g_list_free (windows);
  fclose (out);
  exit (0);
  return G_SOURCE_REMOVE;
}

G_MODULE_EXPORT void gtk_module_init (gint *argc, gchar ***argv)
{
  const char *delay = g_getenv ("LT_PROBE_DELAY");
  g_timeout_add (delay ? atoi (delay) : 4000, dump, NULL);
}
