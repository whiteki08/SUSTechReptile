import json
import pandas as pd
import matplotlib.pyplot as plt

#Step 1: Initialize the environment
plt.rcParams['font.sans-serif'] = ['SimHei']  # Set font to SimHei for Chinese characters
plt.rcParams['axes.unicode_minus'] = False  # Display negative signs correctly
# Step 2: Load the JSON data
with open('GPA.json', 'r', encoding='utf-8') as file:
    data = json.load(file)

# Step 3: Convert JSON data to pandas DataFrame
gpa_data = pd.DataFrame(data['content']['list'])

# Step 4: Clean/preprocess data (if necessary)
# Example: Convert GPA to numeric and handle missing values
gpa_data['zzcj'] = pd.to_numeric(gpa_data['zzcj'], errors='coerce')
gpa_data.dropna(subset=['zzcj'], inplace=True)

# Step 5: Visualize the data
plt.figure(figsize=(10, 8))
plt.bar(gpa_data['kcmc'], gpa_data['zzcj'], color='skyblue')
plt.xlabel('Course Name')
plt.ylabel('GPA')
plt.title('GPA Visualization')
plt.xticks(rotation=90)  # Rotate course names for better readability
plt.tight_layout()  # Adjust layout to make room for the rotated x-axis labels
plt.show()

