# Use a standard Python image
FROM python:3.9-slim

# Set the working directory inside the container
WORKDIR /app

# Install system libraries needed by some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements file first to leverage Docker's build cache
COPY requirements.txt .

# Install all Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Expose the ports for Jupyter and Flask
EXPOSE 8888
EXPOSE 5000

# The command to run when the container starts.
# This starts JupyterLab and makes it accessible.
CMD ["python", "app.py"]