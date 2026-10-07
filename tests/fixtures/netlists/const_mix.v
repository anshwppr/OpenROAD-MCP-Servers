// Gates and a flip-flop tied to constants (the contest netlists only tie DFF RN/SN pins).
module top(a, b, c, clk, y, z, q);
  input a, b, c, clk;
  output y, z, q;
  wire w1, w2, w3;
  AND2 g0(.Y(w1), .A(a), .B(1'b0));
  OR2 g1(.Y(w2), .A(b), .B(1'b1));
  NAND2 g2(.Y(w3), .A(w1), .B(1'b1));
  NOR2 g3(.Y(y), .A(w3), .B(w2));
  XOR2 g4(.Y(z), .A(c), .B(a));
  DFF g5(.CLK(clk), .RN(1'b0), .SN(1'b1), .D(w2), .Q(q));
endmodule
