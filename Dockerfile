FROM php:8.2-cli

WORKDIR /home/container

COPY . /home/container

RUN chmod +x ffmpeg ffprobe \
    && mkdir -p Upload meme tasks \
    && chmod -R 777 /home/container

EXPOSE 10000

CMD ["php", "-S", "0.0.0.0:10000", "-t", "/home/container"]
