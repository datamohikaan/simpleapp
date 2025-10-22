from confluent_kafka  import Producer


# kafka broker
conf = {
        'bootstrap.servers': 'localhost:9092'
        }
#producer instance
producer = Producer(conf)
                  
  
def delivery_report (err, msg):
    if err:
      print(f'Error: {err}') 
    else:
      print(f'Message delivered to {msg.topic()} [{msg.partition()}]')

topic = "test_topic"

for i in range(10):
   message = f"hello kafka!!! message {i}"
   producer.produce(topic, message.encode('utf-8') , callback=delivery_report)
   producer.flush()
   
print ("All messages sent succesfully!")   
   


                    