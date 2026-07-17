# Combined image for the Serverless Agent Portal.
#
# Stage 1 builds the Vite SPA; stage 2 runs the Express server, which serves the
# built SPA from ../frontend/dist and proxies /api. Listens on :8080.

# --- Stage 1: build the React SPA ------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY app/frontend/package*.json ./
RUN npm ci
COPY app/frontend/ ./
RUN npm run build

# --- Stage 2: runtime (Express server + built SPA) -------------------------
FROM node:22-alpine AS runtime
ENV NODE_ENV=production
WORKDIR /app

# Install production server dependencies only.
COPY app/server/package*.json ./server/
RUN cd server && npm ci --omit=dev

# Server source + the built SPA. The server resolves DIST_DIR as
# <server>/../../frontend/dist, so the layout below must be preserved.
COPY app/server/ ./server/
COPY --from=frontend /build/frontend/dist ./frontend/dist

ENV PORT=8080
EXPOSE 8080

# Run as the built-in non-root user for defense-in-depth.
USER node

CMD ["node", "server/src/index.js"]
