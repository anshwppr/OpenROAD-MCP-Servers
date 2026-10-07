// a -> y has articulation gates g0, g3, g4 (g1/g2 form a diamond); w1 and w4 are cuts, w2 is not.
module top(a, b, c, y, z);
  input a, b, c;
  output y, z;
  wire w1, w2, w3, w4;
  BUF g0(.Y(w1), .A(a));
  INV g1(.Y(w2), .A(w1));
  BUF g2(.Y(w3), .A(w1));
  AND2 g3(.Y(w4), .A(w2), .B(w3));
  OR2 g4(.Y(y), .A(w4), .B(b));
  XOR2 g5(.Y(z), .A(w1), .B(c));
endmodule
