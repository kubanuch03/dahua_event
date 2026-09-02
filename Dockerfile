
FROM python:3.8

# Install system dependencies
RUN apt-get update && \
    apt-get install -y libglib2.0-0 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the requirements file and install Python dependencies
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the distribution directory and install the NetSDK package
COPY dist/ dist/
RUN pip install --no-cache-dir dist/NetSDK-2.0.0.1-py3-none-linux_x86_64.whl --verbose

# Copy the rest of your application code
COPY . .

# Command to run the application
CMD ["python", "main.py"]