```
(1) "order-ducks" publishes an "order" message to duck_orders_fanout
              |
              v
   +---------------------------+
   |  duck_orders_fanout       |  (fanout exchange)
   +-------------+-------------+
                 | \
(2) fan out -->  |  \
                 v   v
   +-------------------+         +---------------------+
   |  shipping_queue   | (3)     |  analytics_queue    | (4)
   | (shipping_worker) |         | (analytics_worker)  |
   +--------+----------+         +---------+-----------+
            | (Publishes "shipping_done")  | (Publishes "analytics_done")
            \------------+-----------------/
                         v
                 +------------------+
(5) fan in  -->  | aggregator_queue |
                 +--------+---------+
                          |
                          v
                   +-------------------+
                   | aggregator_worker |
                   +-------------------+
(6)                 (Marks as complete)
```
