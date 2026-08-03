import os
import datetime
import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.cluster import KMeans
import joblib
from database import get_db_connection

MODEL_DIR = os.path.join(os.path.dirname(__file__), 'models')
os.makedirs(MODEL_DIR, exist_ok=True)
FORECAST_MODEL_PATH = os.path.join(MODEL_DIR, 'demand_forecast_model.pkl')

def train_demand_forecasting():
    print("Training Demand Forecasting Model...")
    conn = get_db_connection()
    
    # Get last 30 days of bookings
    query = """
        SELECT DATE(booking_date) as b_date, COUNT(*) as daily_demand
        FROM Bookings
        WHERE booking_date >= date('now', '-30 days')
        GROUP BY DATE(booking_date)
        ORDER BY b_date ASC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    if len(df) < 5:
        print("Not enough data to train forecasting model (needs at least 5 days).")
        return
        
    df['b_date'] = pd.to_datetime(df['b_date'])
    df['day_index'] = (df['b_date'] - df['b_date'].min()).dt.days
    
    X = df[['day_index']]
    y = df['daily_demand']
    
    model = LinearRegression()
    model.fit(X, y)
    
    joblib.dump(model, FORECAST_MODEL_PATH)
    print("Model saved to", FORECAST_MODEL_PATH)
    
    # Predict next 4 weeks (28 days)
    last_day_index = df['day_index'].max()
    future_X = np.array([[last_day_index + i] for i in range(1, 29)])
    predictions = model.predict(future_X)
    
    total_predicted_demand = max(0, sum(predictions))
    
    # Save forecast to DB
    conn = get_db_connection()
    c = conn.cursor()
    today = datetime.date.today()
    period_end = today + datetime.timedelta(days=28)
    
    c.execute('''INSERT INTO Forecasts (forecast_date, period_start, period_end, predicted_demand, confidence)
                 VALUES (?, ?, ?, ?, ?)''', 
              (today.isoformat(), today.isoformat(), period_end.isoformat(), total_predicted_demand, 0.85))
    conn.commit()
    conn.close()
    print("Forecast generated and saved.")

def perform_customer_segmentation():
    print("Performing Customer Segmentation...")
    conn = get_db_connection()
    
    query = """
        SELECT c.customer_id, 
               COUNT(b.booking_id) as booking_frequency, 
               SUM(b.total_amount) as total_spending
        FROM Customers c
        LEFT JOIN Bookings b ON c.customer_id = b.customer_id
        GROUP BY c.customer_id
    """
    df = pd.read_sql_query(query, conn)
    
    if len(df) < 5:
        print("Not enough customers for clustering.")
        conn.close()
        return
        
    # Fill NaN spending with 0
    df['total_spending'] = df['total_spending'].fillna(0)
    
    features = df[['booking_frequency', 'total_spending']]
    
    n_clusters = min(3, len(df))
    kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    df['segment'] = kmeans.fit_predict(features)
    
    # Define labels based on cluster centers (sort by spending)
    centers = pd.DataFrame(kmeans.cluster_centers_, columns=['freq', 'spend'])
    centers['cluster'] = centers.index
    centers = centers.sort_values(by='spend')
    
    labels = {}
    if n_clusters == 3:
        labels = {centers.iloc[0]['cluster']: "Budget Travelers",
                  centers.iloc[1]['cluster']: "Regular Travelers",
                  centers.iloc[2]['cluster']: "Luxury Seekers"}
    elif n_clusters == 2:
        labels = {centers.iloc[0]['cluster']: "Budget Travelers",
                  centers.iloc[1]['cluster']: "Premium Travelers"}
    else:
        labels = {centers.iloc[0]['cluster']: "General Travelers"}
        
    df['segment_label'] = df['segment'].map(labels)
    
    # Update DB (could store this in a new table or update Customers.preferences)
    c = conn.cursor()
    for _, row in df.iterrows():
        c.execute("UPDATE Customers SET preferences = ? WHERE customer_id = ?", 
                  (row['segment_label'], row['customer_id']))
    conn.commit()
    conn.close()
    print("Segmentation completed.")

def run_anomaly_detection():
    print("Running Anomaly Detection...")
    conn = get_db_connection()
    
    # Get last 30 days daily counts
    query = """
        SELECT DATE(booking_date) as b_date, COUNT(*) as daily_demand, SUM(total_amount) as daily_revenue
        FROM Bookings
        WHERE booking_date >= date('now', '-30 days')
        GROUP BY DATE(booking_date)
        ORDER BY b_date ASC
    """
    df = pd.read_sql_query(query, conn)
    
    if len(df) < 5:
        print("Not enough data for anomaly detection.")
        conn.close()
        return
        
    today = datetime.date.today().isoformat()
    if today not in df['b_date'].values:
        # If no bookings today, check for anomaly in 0 bookings
        current_demand = 0
        current_revenue = 0
    else:
        current = df[df['b_date'] == today].iloc[0]
        current_demand = current['daily_demand']
        current_revenue = current['daily_revenue']
        
    mean_demand = df['daily_demand'].mean()
    std_demand = df['daily_demand'].std()
    
    if std_demand == 0 or pd.isna(std_demand):
        conn.close()
        return
        
    z_score = (current_demand - mean_demand) / std_demand
    
    is_anomaly = False
    severity = None
    
    if abs(z_score) > 3:
        is_anomaly = True
        if abs(z_score) >= 5:
            severity = 'red'
        elif abs(z_score) >= 4:
            severity = 'orange'
        else:
            severity = 'yellow'
            
    c = conn.cursor()
    c.execute('''INSERT INTO Analytics_Log (log_date, prediction_value, anomaly_flag, alert_type)
                 VALUES (?, ?, ?, ?)''', 
              (today, z_score, is_anomaly, 'Demand Anomaly' if is_anomaly else 'Normal'))
              
    if is_anomaly:
        desc = f"Unusual booking volume detected. Z-Score: {z_score:.2f}"
        c.execute('''INSERT INTO Alerts (alert_type, description, severity, status)
                     VALUES (?, ?, ?, ?)''',
                  ('Anomaly', desc, severity, 'active'))
                  
    conn.commit()
    conn.close()
    print(f"Anomaly detection finished. Z-score: {z_score:.2f}, Anomaly: {is_anomaly}")

if __name__ == '__main__':
    train_demand_forecasting()
    perform_customer_segmentation()
    run_anomaly_detection()
