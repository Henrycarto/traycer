# Built from the repository root so the workspace lockfile and the SDK
# package are both in context. The dashboard does not import the SDK, but the
# lockfile is shared and installing without it would resolve different
# versions than the ones CI tested.
FROM node:22-alpine AS deps

WORKDIR /repo

COPY package.json package-lock.json turbo.json tsconfig.base.json ./
COPY apps/web/package.json apps/web/package.json
COPY packages/sdk/package.json packages/sdk/package.json

RUN npm ci --workspace=@traycer/web --include-workspace-root


FROM node:22-alpine AS build

WORKDIR /repo

COPY --from=deps /repo/node_modules ./node_modules
COPY --from=deps /repo/apps/web/node_modules ./apps/web/node_modules
COPY package.json package-lock.json turbo.json tsconfig.base.json ./
COPY apps/web ./apps/web

ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build --workspace=@traycer/web


FROM node:22-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

WORKDIR /app

# The standalone output carries only the files the server actually needs,
# which keeps the runtime image small and leaves the build toolchain behind.
COPY --from=build /repo/apps/web/.next/standalone ./
COPY --from=build /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=build /repo/apps/web/public ./apps/web/public

RUN addgroup -g 10001 -S traycer && adduser -u 10001 -S traycer -G traycer
USER traycer

EXPOSE 3000

CMD ["node", "apps/web/server.js"]
