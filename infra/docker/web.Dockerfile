FROM node:20-alpine

WORKDIR /app

COPY apps/web/package*.json ./
RUN npm install

COPY apps/web ./

EXPOSE 3000

CMD ["npm", "run", "dev"]
