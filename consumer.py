from confluent_kafka import Consumer, KafkaException

#
#kafka-console-producer  --topic test_topic  --bootstrap-server localhost:9092           
#>ola
#>how are you doing?
# kafka-console-consumer  --topic test_topic --from-beginning  --bootstrap-server localhost:9092


conf = {
        'bootstrap.servers': 'localhost:9092',
        'group.id': 'my_consumer_group',
        'auto.offset.reset': 'earliest'
        } 

consumer = Consumer(conf)
topic = "test_topic"

consumer.subscribe([topic])
print ("listening for messages ...")

try:
    while True:
        msg = consumer.poll(1.0)
        
        if msg is None:
            continue
        if msg.error():
            raise KafkaException(msg.error())
        
        print(f"Received message: {msg.value().decode('utf-8') }")
        
except KeyboardInterrupt:
       print("\n Stopping consumer..." )

finally:
  consumer.close()                  
