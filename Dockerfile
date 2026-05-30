FROM photo-review-photo-review
WORKDIR /app
COPY app/ .
# Install pyotp + qrcode from vendored packages (no internet needed)
COPY vendor/ /usr/local/lib/python3.12/site-packages/
RUN mkdir -p /app/data /app/data/thumbs
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
