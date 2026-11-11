// Fruchterman-Reingold spring layout with Barnes-Hut repulsion, OpenMP-parallel.
// Same force model, cooling and stop rule as networkx.spring_layout (sparse
// variant), but O(n log n) per iteration instead of O(n^2). Built and loaded by
// scripts/fastlayout.py.
#include <math.h>
#include <stdint.h>
#include <stdlib.h>

typedef struct {
  double cx, cy, mass;  // centre of mass
  double x0, y0, size;  // square bounds
  int child[4];         // -1 when absent
  int body;             // leaf body index, -1 for internal/empty, -2 for merged coincident bodies
} Cell;

typedef struct { Cell *cells; int n, cap; } Tree;

static int new_cell(Tree *t, double x0, double y0, double size) {
  if (t->n == t->cap) { t->cap *= 2; t->cells = realloc(t->cells, sizeof(Cell) * t->cap); }
  Cell *c = &t->cells[t->n];
  c->cx = c->cy = c->mass = 0; c->x0 = x0; c->y0 = y0; c->size = size;
  c->child[0] = c->child[1] = c->child[2] = c->child[3] = -1; c->body = -1;
  return t->n++;
}

static int quadrant(const Cell *c, double x, double y) {
  double h = c->size / 2;
  return (x >= c->x0 + h) + 2 * (y >= c->y0 + h);
}

static void insert(Tree *t, const double *pos, int i) {
  double x = pos[2 * i], y = pos[2 * i + 1];
  int c = 0;
  for (;;) {
    Cell *cell = &t->cells[c];
    // Accumulate mass on the way down.
    cell->cx = (cell->cx * cell->mass + x) / (cell->mass + 1);
    cell->cy = (cell->cy * cell->mass + y) / (cell->mass + 1);
    cell->mass += 1;
    if (cell->mass == 1) { cell->body = i; return; }  // was empty
    if (cell->body == -2) return;                    // coincident pile
    if (cell->body >= 0) {                           // leaf: split
      int old = cell->body;
      if (cell->size < 1e-9) { cell->body = -2; return; }
      cell->body = -1;
      double h = cell->size / 2;
      int q = quadrant(cell, pos[2 * old], pos[2 * old + 1]);
      int nc = new_cell(t, cell->x0 + h * (q & 1), cell->y0 + h * (q >> 1), h);
      cell = &t->cells[c];  // realloc may move
      cell->child[q] = nc;
      Cell *o = &t->cells[nc];
      o->cx = pos[2 * old]; o->cy = pos[2 * old + 1]; o->mass = 1; o->body = old;
    }
    int q = quadrant(cell, x, y);
    if (cell->child[q] < 0) {
      double h = cell->size / 2;
      int nc = new_cell(t, cell->x0 + h * (q & 1), cell->y0 + h * (q >> 1), h);
      t->cells[c].child[q] = nc;
    }
    c = t->cells[c].child[q];
  }
}

static uint64_t splitmix(uint64_t *s) {
  uint64_t z = (*s += 0x9e3779b97f4a7c15ULL);
  z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
  z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
  return z ^ (z >> 31);
}

// pos: n*2 (in: start positions if init != 0, out: layout). Adjacency in CSR
// (offsets n+1, targets, weights), symmetric. Returns iterations run.
int fr_layout(int n, double *pos, int init, const int *offsets, const int *targets, const double *weights,
              double k, int iterations, double theta, double threshold, uint64_t seed, int threads) {
  if (n == 0) return 0;
  if (!init) for (int i = 0; i < 2 * n; i++) pos[i] = (double)(splitmix(&seed) >> 11) / 9007199254740992.0;
  double *disp = malloc(sizeof(double) * 2 * n);
  Tree tree = {malloc(sizeof(Cell) * 4 * (size_t)n + 64), 0, 4 * n + 16};
  double minx = pos[0], maxx = pos[0], miny = pos[1], maxy = pos[1];
  for (int i = 1; i < n; i++) {
    minx = fmin(minx, pos[2 * i]); maxx = fmax(maxx, pos[2 * i]);
    miny = fmin(miny, pos[2 * i + 1]); maxy = fmax(maxy, pos[2 * i + 1]);
  }
  double t = 0.1 * fmax(maxx - minx, maxy - miny), dt = t / (iterations + 1);
  int it;
  for (it = 0; it < iterations; it++) {
    minx = maxx = pos[0]; miny = maxy = pos[1];
    for (int i = 1; i < n; i++) {
      minx = fmin(minx, pos[2 * i]); maxx = fmax(maxx, pos[2 * i]);
      miny = fmin(miny, pos[2 * i + 1]); maxy = fmax(maxy, pos[2 * i + 1]);
    }
    tree.n = 0;
    new_cell(&tree, minx, miny, fmax(maxx - minx, maxy - miny) * 1.0001 + 1e-9);
    for (int i = 0; i < n; i++) insert(&tree, pos, i);
    const Cell *cells = tree.cells;
    double total = 0;
    #pragma omp parallel for schedule(dynamic, 256) num_threads(threads) reduction(+:total)
    for (int i = 0; i < n; i++) {
      double x = pos[2 * i], y = pos[2 * i + 1], fx = 0, fy = 0;
      int stack[256], sp = 0;
      stack[sp++] = 0;
      while (sp) {
        const Cell *c = &cells[stack[--sp]];
        if (c->mass == 0 || c->body == i) continue;
        double dx = x - c->cx, dy = y - c->cy, d2 = dx * dx + dy * dy;
        if (c->body != -1 || c->size * c->size < theta * theta * d2) {
          double mass = c->mass;
          if (c->body == -2 && c->x0 <= x && x < c->x0 + c->size && c->y0 <= y && y < c->y0 + c->size) mass -= 1;  // own pile
          double d = fmax(sqrt(d2), 0.01);
          fx += mass * dx * k * k / (d * d); fy += mass * dy * k * k / (d * d);
        } else {
          for (int q = 0; q < 4; q++) if (c->child[q] >= 0 && sp < 256) stack[sp++] = c->child[q];
        }
      }
      for (int e = offsets[i]; e < offsets[i + 1]; e++) {
        int j = targets[e];
        double dx = x - pos[2 * j], dy = y - pos[2 * j + 1], d = fmax(sqrt(dx * dx + dy * dy), 0.01);
        fx -= dx * weights[e] * d / k; fy -= dy * weights[e] * d / k;
      }
      double len = fmax(sqrt(fx * fx + fy * fy), 0.01);
      disp[2 * i] = fx * t / len; disp[2 * i + 1] = fy * t / len;
      total += disp[2 * i] * disp[2 * i] + disp[2 * i + 1] * disp[2 * i + 1];
    }
    for (int i = 0; i < 2 * n; i++) pos[i] += disp[i];
    t -= dt;
    if (sqrt(total) / n < threshold) { it++; break; }
  }
  free(disp); free(tree.cells);
  return it;
}
